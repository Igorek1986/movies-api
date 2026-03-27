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
from app.api.dependencies import get_current_user, get_device_by_token
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


def _parse_air_date(ep: dict) -> _date | None:
    """Парсит дату выхода эпизода из полей airDateUTC / airDate."""
    for field in ("airDateUTC", "airDate"):
        val = ep.get(field)
        if not val:
            continue
        try:
            return datetime.fromisoformat(val[:10]).date()
        except (ValueError, TypeError):
            pass
    return None


# ─── Шаг 3: линковка TMDB → MyShows ─────────────────────────────────────────

def _normalize(s: str) -> str:
    """Нормализация для сравнения названий."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFD", s)           # é → e + combining accent
    s = re.sub(r"[\u0300-\u036f]", "", s)         # убираем комбинирующие знаки
    s = s.lower().strip()
    s = s.replace("-", " ").replace("_", " ")
    s = re.sub(r"[^\w\s]", "", s)                 # убираем пунктуацию
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def find_myshows_show(mc: MediaCard, client: httpx.AsyncClient, title_en: str | None = None) -> int | None:
    """
    Ищет сериал в MyShows. Порядок:
    1. По imdb_id (shows.GetByExternalId) с верификацией названия (оригинал + английский)
    2. По оригинальному названию + году (shows.GetCatalog)
    3. По английскому названию + году (если передан title_en)
    4. Fallback без года
    Возвращает myshows_show_id или None.
    """
    if not mc.original_title:
        return None

    orig = _normalize(mc.original_title)
    year = mc.year  # строка "2020" или None
    orig_en = _normalize(title_en) if title_en else None

    # 1. Поиск по IMDB ID
    if mc.imdb_id:
        clean_imdb = mc.imdb_id.lstrip("t")  # "tt1234567" → "1234567"
        result = await _ms_rpc(client, "shows.GetByExternalId", {
            "id": int(clean_imdb),
            "source": "imdb",
        })
        if result and isinstance(result, dict):
            found_title = _normalize(result.get("titleOriginal") or result.get("title") or "")
            # Верифицируем по оригинальному или английскому названию
            title_match = (found_title == orig or found_title in orig or orig in found_title or
                           (orig_en and (found_title == orig_en or found_title in orig_en or orig_en in found_title)))
            if found_title and title_match:
                logger.info(f"MyShows link: {mc.card_id} → show_id={result['id']} (imdb)")
                return result["id"]
            else:
                logger.info(f"MyShows link: IMDB match rejected '{found_title}' != '{orig}' / '{orig_en}'")

    # 2. Поиск по оригинальному названию + году
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

    def _title_match(show, query: str, year: str | None) -> bool:
        t = _normalize(show.get("titleOriginal") or show.get("title") or "")
        y = str(show.get("year") or "")
        return t == query and (not year or y == year)

    # Точное совпадение по оригинальному названию + году
    for show in shows:
        if _title_match(show, orig, year):
            logger.info(f"MyShows link: {mc.card_id} → show_id={show['id']} (catalog+year)")
            return show["id"]

    def _search_catalog(query: str, year: str | None):
        return _ms_rpc(client, "shows.GetCatalog", {
            "search": {"query": query, **({"year": int(year)} if year else {})}
        })

    def _extract_shows(result):
        out = []
        for item in (result or []):
            show = item.get("show") if isinstance(item, dict) and "show" in item else item
            if show and isinstance(show, dict):
                out.append(show)
        return out

    def _find_in(shows_list, query: str, with_year: str | None) -> int | None:
        """Точное совпадение с годом, потом без года."""
        for show in shows_list:
            if _title_match(show, query, with_year):
                return show["id"]
        if with_year:
            for show in shows_list:
                t = _normalize(show.get("titleOriginal") or show.get("title") or "")
                if t == query:
                    return show["id"]
        return None

    # Поиск по английскому названию + году (для аниме и нелатинских шоу)
    if orig_en:
        en_result = await _search_catalog(title_en, year)
        en_shows = _extract_shows(en_result)
        sid = _find_in(en_shows, orig_en, year)
        if sid:
            logger.info(f"MyShows link: {mc.card_id} → show_id={sid} (catalog_en+year)")
            return sid

        # Год ±1 для новых шоу (TMDB и MyShows могут расходиться)
        if year:
            for adj_year in (str(int(year) - 1), str(int(year) + 1)):
                adj_result = await _search_catalog(title_en, adj_year)
                adj_shows = _extract_shows(adj_result)
                sid = _find_in(adj_shows, orig_en, adj_year)
                if sid:
                    logger.info(f"MyShows link: {mc.card_id} → show_id={sid} (catalog_en, year±1={adj_year})")
                    return sid

        # Сокращённое название до первого «:» (TMDB часто добавляет подзаголовок)
        short_en = title_en.split(":")[0].strip() if title_en and ":" in title_en else None
        if short_en:
            short_norm = _normalize(short_en)
            short_result = await _search_catalog(short_en, year)
            short_shows = _extract_shows(short_result)
            sid = _find_in(short_shows, short_norm, year)
            if sid:
                logger.info(f"MyShows link: {mc.card_id} → show_id={sid} (catalog_en_short+year)")
                return sid
            if year:
                for adj_year in (str(int(year) - 1), str(int(year) + 1)):
                    adj_result = await _search_catalog(short_en, adj_year)
                    adj_shows = _extract_shows(adj_result)
                    sid = _find_in(adj_shows, short_norm, adj_year)
                    if sid:
                        logger.info(f"MyShows link: {mc.card_id} → show_id={sid} (catalog_en_short, year±1={adj_year})")
                        return sid

    # Fallback: оригинальное название без года
    if year:
        for show in shows:
            t = _normalize(show.get("titleOriginal") or show.get("title") or "")
            if t == orig:
                logger.info(f"MyShows link: {mc.card_id} → show_id={show['id']} (catalog, no year)")
                return show["id"]

    top = [(s.get("titleOriginal") or s.get("title"), s.get("year")) for s in shows[:3]]
    logger.info(f"MyShows link: {mc.card_id} not found for '{mc.original_title}' / '{title_en}' ({year}), catalog top-3: {top}")
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
            next_air = datetime.fromisoformat(mc.next_ep_air_date).replace(tzinfo=None)
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
        if not ep.get("airDate") and not ep.get("airDateUTC"):
            continue  # эпизод без даты — анонс, пропускаем
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
            "air_date":      _parse_air_date(ep),
        })

    if not rows:
        return False

    # Дедупликация: MyShows иногда возвращает один эпизод дважды
    seen: set[tuple] = set()
    deduped = []
    for r in rows:
        key = (r["season"], r["episode"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    rows = deduped

    # Удаляем старые эпизоды шоу и вставляем заново
    await db.execute(delete(Episode).where(Episode.tmdb_show_id == mc.tmdb_id))
    await db.execute(pg_insert(Episode).values(rows))

    mc.episodes_synced_at = datetime.now(timezone.utc)

    # imdb_id / kinopoisk_id — заполняем если ещё пустые
    if not mc.imdb_id:
        raw_imdb = result.get("imdbId")
        if raw_imdb:
            try:
                mc.imdb_id = f"tt{int(raw_imdb):07d}"
            except (ValueError, TypeError):
                pass
    if not mc.kinopoisk_id:
        kp = result.get("kinopoiskId")
        if kp:
            try:
                mc.kinopoisk_id = int(kp)
            except (ValueError, TypeError):
                pass

    # Если TMDB не знает о следующем эпизоде — берём дату из MyShows
    today = _date.today()
    future_dates = [
        r["air_date"] for r in rows
        if r["air_date"] and r["air_date"] > today and not r["is_special"]
    ]
    if future_dates and not mc.next_ep_air_date:
        mc.next_ep_air_date = min(future_dates).isoformat()

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

import asyncio
import re
_CARD_ID_RE = re.compile(r"^(\d+)_(movie|tv)$")

async def _bg_refresh_card(card_id: str) -> None:
    """Фоновое обновление эпизодов одной карточки (вызывается при открытии, раз в сутки)."""
    from app.db.database import async_session_maker
    from app.config import get_settings as _get_settings
    try:
        async with async_session_maker() as db:
            mc = (await db.execute(select(MediaCard).where(MediaCard.card_id == card_id))).scalar_one_or_none()

            # Уже обновляли сегодня — пропускаем
            if mc is not None and mc.episodes_synced_at is not None and mc.episodes_synced_at.date() >= _date.today():
                return

            if mc is None:
                # Карточки нет в БД — создаём через TMDB API
                m = _CARD_ID_RE.match(card_id)
                if not m:
                    return
                tmdb_id = int(m.group(1))
                cfg = _get_settings()
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                        headers={"Authorization": cfg.TMDB_TOKEN},
                        params={"language": "ru-RU"},
                        timeout=15,
                    )
                    if r.status_code != 200:
                        logger.debug(f"_bg_refresh_card {card_id}: TMDB {r.status_code}")
                        return
                    data = r.json()
                    from app.main import upsert_tmdb_cache
                    await upsert_tmdb_cache("tv", tmdb_id, data)
                # Перечитываем после upsert
                mc = (await db.execute(select(MediaCard).where(MediaCard.card_id == card_id))).scalar_one_or_none()
                if not mc:
                    return

            # Линкуем если нужно, затем синхронизируем (минуя _should_sync — rate limit снаружи)
            async with httpx.AsyncClient() as client:
                if mc.myshows_show_id is None:
                    show_id = await find_myshows_show(mc, client)
                    if show_id:
                        mc.myshows_show_id = show_id
                        await db.commit()
                    else:
                        return
                await sync_episodes(mc, db, client)
    except Exception as e:
        logger.debug(f"_bg_refresh_card {card_id}: {e}")


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

    # Фоновый refresh раз в сутки при открытии карточки (линковка + обновление эпизодов)
    asyncio.create_task(_bg_refresh_card(card_id))

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
    today = _date.today()
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

        future = bool(ep.air_date and ep.air_date > today)
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
            "future":       future,
            "air_date":     ep.air_date.isoformat() if ep.air_date else None,
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


@router.get("/api/refresh-card-episodes")
async def api_refresh_card_episodes(
    card_id: str = Query(...),
    device: "Device" = Depends(get_device_by_token),
):
    """Fire-and-forget обновление эпизодов одной карточки (из плагина при открытии)."""
    if not device:
        raise HTTPException(status_code=401)
    m = _CARD_ID_RE.match(card_id)
    if not m or m.group(2) != "tv":
        return {"ok": False}
    asyncio.create_task(_bg_refresh_card(card_id))
    return {"ok": True}
