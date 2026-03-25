"""
Эпизоды сериалов: синхронизация с MyShows и эндпоинт /api/episodes.

Шаг 3. find_myshows_show(mc, client)  — линковка MediaCard → myshows_show_id
Шаг 4. sync_episodes(mc, db, client)  — заполнение таблицы episodes из MyShows
Шаг 5. GET /api/episodes              — ленивая синхронизация + таблица
"""
import json
import logging
from datetime import date as _date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.database import get_db
from app.db.models import Device, Episode, MediaCard, Timecode, User
from app.api.dependencies import get_current_user
from app.utils import lampa_hash, build_episode_hash_string

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

_WATCHED_PCT = 90


# ─── MyShows public RPC (без авторизации) ────────────────────────────────────

async def _ms_rpc(client: httpx.AsyncClient, method: str, params: dict) -> dict | None:
    """JSON-RPC запрос к MyShows без токена (только публичные методы)."""
    try:
        resp = await client.post(
            settings.MYSHOWS_API,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if "error" in data:
            logger.debug(f"MyShows RPC {method} error: {data['error']}")
            return None
        return data.get("result")
    except Exception as e:
        logger.debug(f"MyShows RPC {method} failed: {e}")
        return None


# ─── Шаг 3: линковка TMDB → MyShows ─────────────────────────────────────────

def _normalize(s: str) -> str:
    """Упрощённая нормализация для сравнения названий."""
    return s.lower().strip()


async def find_myshows_show(mc: MediaCard, client: httpx.AsyncClient) -> int | None:
    """
    Ищет сериал в MyShows. Порядок:
    1. По imdb_id (shows.GetByExternalId) с верификацией названия
    2. По названию + году (shows.GetCatalog)
    3. По названию без года (fallback)
    Возвращает myshows_show_id или None.
    """
    if not mc.original_title:
        return None

    orig = _normalize(mc.original_title)
    year = mc.year  # строка "2020" или None

    # 1. Поиск по IMDB ID
    if mc.imdb_id:
        clean_imdb = mc.imdb_id.lstrip("t")  # "tt1234567" → "1234567"
        result = await _ms_rpc(client, "shows.GetByExternalId", {
            "id": int(clean_imdb),
            "source": "imdb",
        })
        if result and isinstance(result, dict):
            found_title = _normalize(result.get("titleOriginal") or result.get("title") or "")
            # Верифицируем название (MyShows иногда возвращает неверные совпадения по IMDB)
            if found_title and (found_title == orig or found_title in orig or orig in found_title):
                logger.info(f"MyShows link: {mc.card_id} → show_id={result['id']} (imdb)")
                return result["id"]
            else:
                logger.debug(f"MyShows link: IMDB match rejected '{found_title}' != '{orig}'")

    # 2. Поиск по каталогу с годом
    params: dict = {"search": {"query": mc.original_title}}
    if year:
        params["search"]["year"] = int(year)

    result = await _ms_rpc(client, "shows.GetCatalog", params)
    if not result:
        return None

    shows = []
    for item in result:
        show = item.get("show") if isinstance(item, dict) and "show" in item else item
        if show and isinstance(show, dict):
            shows.append(show)

    # Точное совпадение по названию и году
    for show in shows:
        title_orig = _normalize(show.get("titleOriginal") or show.get("title") or "")
        show_year = str(show.get("year") or "")
        if title_orig == orig and (not year or show_year == year):
            logger.info(f"MyShows link: {mc.card_id} → show_id={show['id']} (catalog+year)")
            return show["id"]

    # 3. Fallback: только по названию без года
    if year:
        for show in shows:
            title_orig = _normalize(show.get("titleOriginal") or show.get("title") or "")
            if title_orig == orig:
                logger.info(f"MyShows link: {mc.card_id} → show_id={show['id']} (catalog, no year)")
                return show["id"]

    logger.debug(f"MyShows link: {mc.card_id} not found for '{mc.original_title}' ({year})")
    return None


# ─── Шаг 4: синхронизация эпизодов ──────────────────────────────────────────

def _should_sync(mc: MediaCard) -> bool:
    """
    Проверяет нужна ли синхронизация эпизодов по логике из плана:
    - episodes_synced_at IS NULL → никогда не синхронизировали
    - next_ep_air_date == "" → сериал завершён → синхронизируем один раз
    - next_ep_air_date != "" → сериал в эфире → обновляем если вышел новый эпизод
    """
    if mc.myshows_show_id is None:
        return False
    if mc.episodes_synced_at is None:
        return True
    if mc.next_ep_air_date == "":
        # завершён — уже синхронизировали, не трогаем
        return False
    if mc.next_ep_air_date:
        # онгоинг: перепроверяем только если новый эпизод уже вышел и мы не синхронизировали после него
        synced_date = mc.episodes_synced_at.replace(tzinfo=None)
        try:
            next_air = datetime.fromisoformat(mc.next_ep_air_date)
            now = datetime.now()
            if next_air <= now and synced_date < next_air:
                return True
        except Exception:
            pass
    return False


async def sync_episodes(mc: MediaCard, db: AsyncSession, client: httpx.AsyncClient) -> bool:
    """
    Синхронизирует эпизоды из MyShows в таблицу episodes.
    Возвращает True если синхронизация прошла успешно.
    """
    result = await _ms_rpc(client, "shows.GetById", {
        "showId": mc.myshows_show_id,
        "withEpisodes": True,
    })
    if not result:
        return False

    raw_episodes = result.get("episodes") or []
    if not raw_episodes:
        return False

    rows = []
    for ep in raw_episodes:
        snum = ep.get("seasonNumber")
        enum = ep.get("episodeNumber")
        if snum is None or enum is None:
            continue
        runtime_min = ep.get("runtime") or 0
        duration_sec = runtime_min * 60 if runtime_min else None
        orig = mc.original_title or ""
        rows.append({
            "tmdb_show_id":  mc.tmdb_id,
            "season":        snum,
            "episode":       enum,
            "title":         ep.get("title") or None,
            "duration_sec":  duration_sec,
            "is_special":    bool(ep.get("isSpecial", False)) or enum == 0 or snum == 0,
            "myshows_ep_id": ep.get("id"),
            "hash":          lampa_hash(build_episode_hash_string(snum, enum, orig)),
        })

    if not rows:
        return False

    # Удаляем старые эпизоды шоу и вставляем заново
    await db.execute(delete(Episode).where(Episode.tmdb_show_id == mc.tmdb_id))
    await db.execute(pg_insert(Episode).values(rows))

    mc.episodes_synced_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"sync_episodes: {mc.card_id} → {len(rows)} episodes synced")
    return True


async def _ensure_synced(mc: MediaCard, db: AsyncSession) -> bool:
    """Линкует + синхронизирует эпизоды если нужно. Возвращает True если таблица заполнена."""
    async with httpx.AsyncClient() as client:
        # Если show_id ещё не проставлен — линкуем
        if mc.myshows_show_id is None:
            show_id = await find_myshows_show(mc, client)
            if not show_id:
                return False
            mc.myshows_show_id = show_id
            await db.commit()

        # Синхронизируем если нужно
        if _should_sync(mc):
            await sync_episodes(mc, db, client)

    # Проверяем что в таблице что-то есть
    count = await db.scalar(
        select(Episode).where(Episode.tmdb_show_id == mc.tmdb_id).limit(1)
    )
    return count is not None


# ─── Шаг 5: /api/episodes ────────────────────────────────────────────────────

import re
_CARD_ID_RE = re.compile(r"^(\d+)_(movie|tv)$")


@router.get("/api/episodes")
async def api_episodes(
    device_id: int = Query(...),
    card_id: str = Query(...),
    profile_id: str | None = Query(None),
    include_specials: int = Query(0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает список вышедших серий сериала с хэшами и флагом watched.
    Если таблица episodes заполнена — использует её (фильтр is_special).
    Иначе fallback на TMDB seasons_json.
    """
    if not current_user:
        raise HTTPException(status_code=401)

    m = _CARD_ID_RE.match(card_id)
    if not m or m.group(2) != "tv":
        raise HTTPException(status_code=400, detail="Только для сериалов")

    mc_result = await db.execute(select(MediaCard).where(MediaCard.card_id == card_id))
    mc = mc_result.scalar_one_or_none()
    if not mc or not mc.original_title:
        return {"episodes": []}

    # Загружаем таймкоды
    tc_where = [Timecode.device_id == device_id, Timecode.card_id == card_id]
    if profile_id is not None:
        tc_where.append(Timecode.lampa_profile_id == profile_id)
    tc_result = await db.execute(select(Timecode.item, Timecode.data).where(*tc_where))
    watched_items: set[str] = set()
    special_items: set[str] = set()
    timecode_data: dict[str, dict] = {}
    for item, data_raw in tc_result.all():
        try:
            d = json.loads(data_raw)
            pct = d.get("percent", 0)
            timecode_data[item] = d
            if pct >= _WATCHED_PCT:
                watched_items.add(item)
            if d.get("special"):
                special_items.add(item)
        except Exception:
            pass

    orig_title = mc.original_title

    # Пробуем синхронизировать и использовать таблицу episodes
    has_ep_table = await _ensure_synced(mc, db)

    if has_ep_table:
        return await _episodes_from_table(mc, db, orig_title, watched_items, special_items, timecode_data, include_specials)

    # Fallback: TMDB seasons_json
    return _episodes_from_tmdb(mc, orig_title, watched_items, special_items, timecode_data)


async def _episodes_from_table(
    mc: MediaCard,
    db: AsyncSession,
    orig_title: str,
    watched_items: set,
    special_items: set,
    timecode_data: dict,
    include_specials: int,
) -> dict:
    """Строит список эпизодов из таблицы episodes.
    Спешлы всегда включаются в список (с пометкой special=True),
    но не учитываются в счётчике watched/total на карточке.
    """
    query = (
        select(Episode)
        .where(Episode.tmdb_show_id == mc.tmdb_id)
        .order_by(Episode.season, Episode.episode)
    )
    result = await db.execute(query)
    db_episodes = result.scalars().all()

    episodes = []
    for ep in db_episodes:
        snum, enum = ep.season, ep.episode

        # season=0 — спешлы без сезона в MyShows: показываем только если include_specials
        if snum == 0 and not include_specials:
            continue

        h = lampa_hash(build_episode_hash_string(snum, enum, orig_title))
        td = timecode_data.get(h, {})
        duration_sec = ep.duration_sec or td.get("duration") or ((mc.episode_run_time * 60) if mc.episode_run_time else None)

        episodes.append({
            "season":       snum,
            "episode":      enum,
            "title":        ep.title,
            "hash":         h,
            "watched":      h in watched_items,
            "special":      ep.is_special or h in special_items,
            "percent":      td.get("percent", 0),
            "duration_sec": duration_sec,
        })

    return {"episodes": episodes, "original_title": orig_title, "source": "myshows"}


def _episodes_from_tmdb(
    mc: MediaCard,
    orig_title: str,
    watched_items: set,
    special_items: set,
    timecode_data: dict,
) -> dict:
    """Fallback: строит список эпизодов из TMDB seasons_json."""
    if not mc.seasons_json:
        return {"episodes": []}

    try:
        seasons = json.loads(mc.seasons_json)
    except Exception:
        return {"episodes": []}

    last_s = mc.last_ep_season or 0
    last_e = mc.last_ep_number or 0
    today_str = _date.today().isoformat()
    duration_sec = (mc.episode_run_time * 60) if mc.episode_run_time else None

    episodes = []
    for s in seasons:
        snum = s.get("season_number") or 0
        if snum == 0:
            continue
        ep_count = s.get("episode_count") or 0

        if last_s > 0:
            if snum < last_s:
                aired_to = ep_count
            elif snum == last_s:
                aired_to = last_e
            else:
                continue
        else:
            s_air = s.get("air_date") or ""
            if s_air and s_air <= today_str:
                aired_to = ep_count
            else:
                continue

        for ep in range(1, aired_to + 1):
            h = lampa_hash(build_episode_hash_string(snum, ep, orig_title))
            td = timecode_data.get(h, {})
            episodes.append({
                "season":       snum,
                "episode":      ep,
                "hash":         h,
                "watched":      h in watched_items,
                "special":      h in special_items,
                "percent":      td.get("percent", 0),
                "duration_sec": duration_sec or td.get("duration"),
            })

    return {"episodes": episodes, "original_title": orig_title, "source": "tmdb"}
