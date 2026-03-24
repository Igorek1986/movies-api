"""
Таймкоды — прогресс просмотра, привязанный к устройству и профилю Lampa.

Форматы данных:
  Внутренний (БД):
    Timecode(device_id, lampa_profile_id, card_id, item, data)
    card_id = "{tmdb_id}_movie" | "{tmdb_id}_tv"
    item    = строка от lampa_hash()
    data    = JSON: {"time": N, "duration": N, "percent": N}

  Экспорт / Lampac all_views:
    {"123_movie": {"hash1": '{"percent":100,...}'}, ...}

  Lampa file_view:
    {"hash1": {"duration": N, "time": N, "percent": N, "profile": 0}, ...}
    (нет card_id — при импорте card_id="lampa_import")
"""

import asyncio
import json
import logging
import re
import secrets
from datetime import date
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Body, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select, delete, func, update

from app.config import get_settings
from app.db.database import get_db, async_session_maker
from app import rate_limit
from app.db.models import Device, Timecode, MediaCard, LampaProfile, User
from app.api.dependencies import get_device_by_token
from app import settings_cache
from app.ws_manager import manager as ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/timecode", tags=["timecodes"])


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _require_device(device: Device | None) -> Device:
    if not device:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий token")
    return device


async def _check_import_rate_limit(device: Device, db: AsyncSession) -> None:
    """Проверяет дневной лимит JSON-импорта по роли пользователя."""
    user = await db.get(User, device.user_id)
    if not user:
        return
    daily_limit = settings_cache.get_role_limit(user.role, "import_daily")
    if daily_limit is None:
        return  # super — без ограничений
    allowed, wait_sec = rate_limit.check_import(device.user_id, daily_limit)
    if not allowed:
        h, m = divmod(wait_sec // 60, 60)
        wait_str = f"{h} ч {m} мин" if h else f"{m} мин"
        raise HTTPException(
            status_code=429,
            detail=f"Лимит импорта исчерпан. Повторите через {wait_str}.",
        )


async def _assert_profile_allowed(device: Device, profile_id: str, db: AsyncSession) -> None:
    """Проверяет лимит профилей. Бросает 403 если профиль новый и лимит исчерпан."""
    user = (await db.execute(select(User).where(User.id == device.user_id))).scalar_one_or_none()
    role = user.role if user else "simple"
    limit = settings_cache.get_role_limit(role, "profile_limit") or 3
    if limit is None:
        return  # super — без лимита

    if profile_id:
        # Именованный профиль: если уже существует — всё ок
        existing = (await db.execute(
            select(LampaProfile).where(
                LampaProfile.device_id == device.id,
                LampaProfile.lampa_profile_id == profile_id,
            )
        )).scalar_one_or_none()
        if existing:
            return
    else:
        # Основной (profile_id=""): если уже есть таймкоды — всё ок
        has_tc = (await db.execute(
            select(func.count()).select_from(Timecode).where(
                Timecode.device_id == device.id,
                Timecode.lampa_profile_id == "",
            )
        )).scalar() or 0
        if has_tc > 0:
            return

    # Новый слот — проверяем лимит
    lp_count = (await db.execute(
        select(func.count()).select_from(LampaProfile)
        .where(LampaProfile.device_id == device.id)
    )).scalar() or 0
    if lp_count >= limit:
        raise HTTPException(status_code=403, detail="Достигнут лимит профилей")


async def _get_user_role(device: Device, db: AsyncSession) -> str:
    """Возвращает роль пользователя устройства."""
    user = await db.get(User, device.user_id)
    return user.role if user else "simple"


async def _trim_to_limit(
    db: AsyncSession,
    device_id: int,
    lampa_profile_id: str,
    user_role: str,
) -> int:
    """
    Удаляет самые старые таймкоды если их количество превышает лимит роли.
    Возвращает количество удалённых записей.
    """
    limit = settings_cache.get_role_limit(user_role, "timecode_limit")
    if limit is None:
        return 0  # super — без ограничений

    count = (await db.execute(
        select(func.count()).select_from(Timecode).where(
            Timecode.device_id == device_id,
            Timecode.lampa_profile_id == lampa_profile_id,
        )
    )).scalar() or 0

    excess = count - limit
    if excess <= 0:
        return 0

    oldest_ids = (await db.execute(
        select(Timecode.id)
        .where(
            Timecode.device_id == device_id,
            Timecode.lampa_profile_id == lampa_profile_id,
        )
        .order_by(Timecode.updated_at.asc())
        .limit(excess)
    )).scalars().all()

    if oldest_ids:
        await db.execute(delete(Timecode).where(Timecode.id.in_(oldest_ids)))
        await db.commit()
        logger.info(
            f"Trimmed {len(oldest_ids)} timecodes: device={device_id} "
            f"profile={lampa_profile_id!r} role={user_role} limit={limit}"
        )
    return len(oldest_ids)


def _media_card_to_entry(mc) -> dict:
    """
    Конвертирует MediaCard в card-объект для Lampa favorite.
    Ключевой момент: для TV сериалов Lampa использует original_name (не original_title)
    чтобы определить тип — router.js: data.original_name ? 'tv' : 'movie'
    """
    entry = {
        "id": mc.tmdb_id,
        "type": mc.media_type,
        "poster_path": mc.poster_path or "",
        "backdrop_path": mc.backdrop_path or "",
        "vote_average": mc.vote_average or 0,
        "overview": mc.overview or "",
        "source": "tmdb",
    }
    if mc.media_type == "tv":
        entry["name"] = mc.title or ""
        entry["original_name"] = mc.original_title or ""  # ключевое поле для роутинга
        entry["first_air_date"] = mc.release_date or ""
        if mc.number_of_seasons:
            entry["number_of_seasons"] = mc.number_of_seasons
        if mc.number_of_episodes:
            entry["number_of_episodes"] = mc.number_of_episodes
        if mc.next_ep_air_date:
            entry["next_episode_to_air"] = {"air_date": mc.next_ep_air_date}
    else:
        entry["title"] = mc.title or ""
        entry["original_title"] = mc.original_title or ""
        entry["release_date"] = mc.release_date or ""
    return entry


async def _merge_favorite_history(
    db: AsyncSession,
    device_id: int,
    profile_id: str,
    entries: list[dict],
    user_role: str = "simple",
) -> None:
    """
    Добавляет записи в favorite Lampa-формата:
      history — список TMDB ID (int)
      card    — список объектов с метаданными карточек
    Не перезаписывает существующие записи (дедупликация по tmdb_id).
    Применяет лимит по роли пользователя.
    Не делает commit — вызывающий код коммитит сам.
    """
    if not entries:
        return

    lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device_id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()

    if not lp:
        lp = LampaProfile(device_id=device_id, lampa_profile_id=profile_id, name="")
        db.add(lp)
        await db.flush()

    try:
        existing_fav = json.loads(lp.favorite) if lp.favorite else {}
    except Exception:
        existing_fav = {}

    existing_history = existing_fav.get("history", [])   # list of int tmdb_id
    existing_cards   = existing_fav.get("card", [])       # list of card objects

    existing_ids      = set(existing_history)
    existing_card_ids = {c.get("id") for c in existing_cards if c.get("id")}

    new_ids   = []
    new_cards = []
    for e in entries:
        tmdb_id = e.get("id")
        if not tmdb_id:
            continue
        if tmdb_id not in existing_ids:
            existing_ids.add(tmdb_id)
            new_ids.append(tmdb_id)
        if tmdb_id not in existing_card_ids:
            existing_card_ids.add(tmdb_id)
            new_cards.append(e)

    if not new_ids:
        return

    existing_fav["history"] = new_ids + existing_history
    existing_fav["card"]    = new_cards + existing_cards

    limit = settings_cache.get_role_limit(user_role, "favorite_limit")
    if limit is not None:
        existing_fav = _trim_favorite(existing_fav, limit)

    lp.favorite = json.dumps(existing_fav, ensure_ascii=False)


async def _upsert_timecodes(
    db: AsyncSession,
    device_id: int,
    lampa_profile_id: str,
    rows: list[dict],
):
    """UPSERT списка таймкодов. rows: [{card_id, item, data}]"""
    if not rows:
        return 0

    # Дедупликация: последний побеждает
    unique: dict[tuple, dict] = {}
    for r in rows:
        unique[(r["card_id"], r["item"])] = r

    values = [
        {
            "device_id": device_id,
            "lampa_profile_id": lampa_profile_id,
            "card_id": r["card_id"],
            "item": r["item"],
            "data": r["data"],
        }
        for r in unique.values()
    ]

    stmt = pg_insert(Timecode).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            Timecode.device_id, Timecode.lampa_profile_id, Timecode.card_id, Timecode.item
        ],
        set_={"data": stmt.excluded.data, "updated_at": stmt.excluded.updated_at},
    )
    await db.execute(stmt)
    await db.commit()
    return len(values)


# ---------------------------------------------------------------------------
# Загрузка таймкодов устройства в память (используется для фильтрации в main.py)
# ---------------------------------------------------------------------------

async def load_device_timecodes(
    db: AsyncSession,
    device_id: int,
    lampa_profile_id: str = "",
) -> dict[str, dict[str, str]]:
    """
    Возвращает словарь {card_id: {item: data_json_string}}.
    Тот же формат что у Lampac /timecode/all_views.
    """
    result = await db.execute(
        select(Timecode).where(
            Timecode.device_id == device_id,
            Timecode.lampa_profile_id == lampa_profile_id,
        )
    )
    rows = result.scalars().all()

    out: dict[str, dict[str, str]] = {}
    for tc in rows:
        out.setdefault(tc.card_id, {})[tc.item] = tc.data
    return out


def get_watched_movie_ids(
    timecodes: dict[str, dict[str, str]],
    threshold: int | None = None,
) -> set[str]:
    """Возвращает card_id фильмов, где хоть один таймкод >= threshold."""
    if threshold is None:
        threshold = settings_cache.get_int("watched_threshold")
    watched = set()
    for card_id, items in timecodes.items():
        if card_id.endswith("_tv"):
            continue
        for data_str in items.values():
            try:
                if json.loads(data_str).get("percent", 0) >= threshold:
                    watched.add(card_id)
                    break
            except (json.JSONDecodeError, TypeError):
                continue
    return watched


# ---------------------------------------------------------------------------
# Фоновое получение TMDB-метаданных при сохранении таймкода
# ---------------------------------------------------------------------------

_CARD_ID_RE = re.compile(r"^(\d+)_(movie|tv)$")


async def _fetch_and_store_media_card(
    card_id: str, tmdb_id: int, media_type: str,
    device_id: int | None = None, lampa_profile_id: str | None = None,
) -> None:
    """Фоновая задача: получает метаданные из TMDB и сохраняет/обновляет в media_cards.
    Если переданы device_id/lampa_profile_id — обновляет updated_at таймкодов датой выхода."""
    settings = get_settings()
    headers = {"Authorization": settings.TMDB_TOKEN, "Accept": "application/json"}
    endpoint = "tv" if media_type == "tv" else "movie"
    title_key = "name" if media_type == "tv" else "title"
    orig_key = "original_name" if media_type == "tv" else "original_title"
    date_key = "first_air_date" if media_type == "tv" else "release_date"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
                headers=headers,
                params={"language": "ru-RU"},
            )
            if resp.status_code != 200:
                return
            data = resp.json()

        date_val = data.get(date_key) or ""
        values: dict = {
            "card_id": card_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": data.get(title_key) or "",
            "original_title": data.get(orig_key) or "",
            "poster_path": data.get("poster_path") or "",
            "year": date_val[:4],
            "backdrop_path": data.get("backdrop_path") or "",
            "overview": data.get("overview") or "",
            "vote_average": data.get("vote_average"),
            "release_date": date_val,
        }
        if media_type == "tv":
            seasons = data.get("seasons")
            values["last_air_date"] = data.get("last_air_date") or ""
            values["number_of_seasons"] = data.get("number_of_seasons")
            values["number_of_episodes"] = data.get("number_of_episodes")
            values["seasons_json"] = json.dumps(seasons, ensure_ascii=False) if seasons else None
            last_ep = data.get("last_episode_to_air") or {}
            values["last_ep_season"] = last_ep.get("season_number")
            values["last_ep_number"] = last_ep.get("episode_number")
            values["next_ep_air_date"] = (data.get("next_episode_to_air") or {}).get("air_date") or ""

        async with async_session_maker() as db:
            mc_stmt = pg_insert(MediaCard).values([values])
            mc_stmt = mc_stmt.on_conflict_do_update(
                index_elements=["card_id"],
                set_={k: mc_stmt.excluded[k] for k in values if k != "card_id"},
            )
            await db.execute(mc_stmt)

            # Обновляем дату таймкодов при импорте (не при сохранении из плагина)
            if device_id is not None:
                date_str = (
                    values.get("last_air_date") if media_type == "tv"
                    else values.get("release_date")
                ) or ""
                if date_str:
                    try:
                        from datetime import datetime
                        watch_date = datetime.fromisoformat(date_str)
                        await db.execute(
                            update(Timecode)
                            .where(
                                Timecode.device_id == device_id,
                                Timecode.card_id == card_id,
                                Timecode.lampa_profile_id == (lampa_profile_id or ""),
                            )
                            .values(updated_at=watch_date)
                        )
                    except (ValueError, TypeError):
                        pass

            await db.commit()
        logger.debug(f"MediaCard saved: {card_id}")
    except Exception as e:
        logger.warning(f"MediaCard fetch failed for {card_id}: {e}")


# ---------------------------------------------------------------------------
# Сохранение таймкода из плагина (при выходе из плеера)
# ---------------------------------------------------------------------------

@router.post("")
async def save_timecode(
    card_id: str = Body(...),
    item: str = Body(...),
    data: str = Body(...),
    profile_id: str = Query(None),
    profile_name: str = Query(None),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Плагин отправляет прогресс просмотра при выходе из плеера.
    Body: {card_id, item, data}  где data — JSON-строка {time, duration, percent}.
    ?profile_id=   — опциональный ID профиля Lampa.
    ?profile_name= — человеческое название профиля (из Lampa.Account.Permit).
    """
    _require_device(device)

    try:
        json.loads(data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="data должна быть JSON-строкой")

    lampa_profile_id = profile_id or ""
    await _assert_profile_allowed(device, lampa_profile_id, db)

    user_role = await _get_user_role(device, db)
    await _upsert_timecodes(
        db, device.id, lampa_profile_id,
        [{"card_id": card_id, "item": item, "data": data}]
    )
    await _trim_to_limit(db, device.id, lampa_profile_id, user_role)

    # Авто-сохраняем имя профиля если передано и профиль не дефолтный
    if lampa_profile_id and profile_name:
        name = profile_name.strip()[:100]
        stmt = pg_insert(LampaProfile).values(
            device_id=device.id,
            lampa_profile_id=lampa_profile_id,
            name=name,
        ).on_conflict_do_update(
            constraint="uq_lampa_profile",
            set_={"name": name},
        )
        await db.execute(stmt)
        await db.commit()

    logger.debug(f"Timecode saved: device={device.id}, profile={lampa_profile_id!r}, card={card_id}")

    m = _CARD_ID_RE.match(card_id)
    if m:
        asyncio.create_task(_fetch_and_store_media_card(card_id, int(m.group(1)), m.group(2)))

    # Рассылаем обновление другим соединениям того же пользователя
    asyncio.create_task(ws_manager.broadcast(
        device.user_id, None,  # None = отправить всем (HTTP-запрос не знает conn_id)
        {"type": "timecode", "profile_id": lampa_profile_id, "card_id": card_id, "item": item, "data": data},
    ))

    return {"success": True}


# ---------------------------------------------------------------------------
# Пакетный импорт
# ---------------------------------------------------------------------------

@router.post("/batch")
async def batch_save_timecodes(
    timecodes: list[dict] = Body(...),
    profile_id: str = Query(None),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Пакетный UPSERT таймкодов.
    Body: [{card_id, item, data}, ...]
    """
    _require_device(device)

    rows = []
    for tc in timecodes:
        if not tc.get("card_id") or not tc.get("item") or not tc.get("data"):
            continue
        rows.append({"card_id": tc["card_id"], "item": tc["item"], "data": tc["data"]})

    user_role = await _get_user_role(device, db)
    saved = await _upsert_timecodes(db, device.id, profile_id or "", rows)
    await _trim_to_limit(db, device.id, profile_id or "", user_role)
    return {"success": True, "saved": saved}


# ---------------------------------------------------------------------------
# Экспорт — формат совместим с Lampac /timecode/all_views
# ---------------------------------------------------------------------------

@router.get("/export")
async def export_timecodes(
    profile_id: str = Query(None),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Экспорт всех таймкодов устройства (с учётом profile_id).
    Формат: {card_id: {item: data_json_string}}
    """
    _require_device(device)
    timecodes = await load_device_timecodes(db, device.id, profile_id or "")
    return timecodes


# ---------------------------------------------------------------------------
# Импорт из Lampac (формат all_views)
# ---------------------------------------------------------------------------

@router.post("/import/lampac")
async def import_from_lampac(
    data: dict[str, dict[str, str]] = Body(...),
    profile_id: str = Query(None),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт из Lampac /timecode/all_views.
    Body: {"123_movie": {"hash1": '{"percent":100,...}'}, ...}
    """
    _require_device(device)
    await _check_import_rate_limit(device, db)
    await _assert_profile_allowed(device, profile_id or "", db)

    rows = []
    for card_id, items in data.items():
        for item, tc_data in items.items():
            rows.append({"card_id": card_id, "item": item, "data": tc_data})

    user_role = await _get_user_role(device, db)
    saved = await _upsert_timecodes(db, device.id, profile_id or "", rows)
    trimmed = await _trim_to_limit(db, device.id, profile_id or "", user_role)
    logger.info(f"Lampac import: device={device.id}, saved={saved}, trimmed={trimmed}")

    # Запускаем фоновую загрузку MediaCard + обновление даты таймкодов
    lp = profile_id or ""
    valid_card_ids = []
    for card_id in data.keys():
        m = _CARD_ID_RE.match(card_id)
        if m:
            valid_card_ids.append(card_id)
            asyncio.create_task(_fetch_and_store_media_card(
                card_id, int(m.group(1)), m.group(2), device.id, lp,
            ))

    # Обновляем favorite.history один раз по уже существующим в DB MediaCards.
    # Новые карточки появятся в истории при следующем импорте (после обогащения TMDB).
    if valid_card_ids:
        mc_result = await db.execute(
            select(MediaCard).where(MediaCard.card_id.in_(valid_card_ids))
        )
        entries = [_media_card_to_entry(mc) for mc in mc_result.scalars().all()]
        if entries:
            await _merge_favorite_history(db, device.id, lp, entries, user_role)
            await db.commit()

    return {"success": True, "saved": saved, "trimmed": trimmed}


# ---------------------------------------------------------------------------
# Импорт из Lampa localStorage (ключ file_view)
# ---------------------------------------------------------------------------

@router.post("/import/lampa")
async def import_from_lampa(
    data: dict[str, Any] = Body(...),
    profile_id: str = Query(None),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт из Lampa localStorage['file_view'].
    Body: {"572566331": {"duration": 6450, "time": 2715, "percent": 42, "profile": 0}, ...}

    В Lampa формате нет card_id — хранится с card_id="lampa_import".
    """
    _require_device(device)
    await _check_import_rate_limit(device, db)
    await _assert_profile_allowed(device, profile_id or "", db)

    rows = []
    for item_hash, tc_data in data.items():
        if not isinstance(tc_data, dict):
            continue
        normalized = {
            "time": tc_data.get("time", 0),
            "duration": tc_data.get("duration", 0),
            "percent": tc_data.get("percent", 0),
        }
        rows.append({
            "card_id": "lampa_import",
            "item": str(item_hash),
            "data": json.dumps(normalized),
        })

    user_role = await _get_user_role(device, db)
    saved = await _upsert_timecodes(db, device.id, profile_id or "", rows)
    trimmed = await _trim_to_limit(db, device.id, profile_id or "", user_role)
    logger.info(f"Lampa import: device={device.id}, saved={saved}, trimmed={trimmed}")
    return {
        "success": True,
        "saved": saved,
        "trimmed": trimmed,
        "note": "Импортировано без card_id. Для серверной фильтрации используйте MyShows sync.",
    }


# ---------------------------------------------------------------------------
# Удаление таймкода
# ---------------------------------------------------------------------------

@router.delete("")
async def delete_timecode(
    card_id: str = Query(...),
    item: str = Query(...),
    profile_id: str = Query(None),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """Удалить конкретный таймкод."""
    _require_device(device)

    await db.execute(
        delete(Timecode).where(
            Timecode.device_id == device.id,
            Timecode.lampa_profile_id == (profile_id or ""),
            Timecode.card_id == card_id,
            Timecode.item == item,
        )
    )
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# История просмотра
# ---------------------------------------------------------------------------

@router.get("/history")
async def get_watch_history(
    profile_id: str = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    История просмотра устройства (с учётом profile_id).
    Возвращает список карточек с метаданными TMDB, отсортированных по дате просмотра.
    """
    _require_device(device)

    result = await db.execute(
        select(Timecode)
        .where(
            Timecode.device_id == device.id,
            Timecode.lampa_profile_id == (profile_id or ""),
        )
        .order_by(Timecode.updated_at.desc())
    )
    timecodes = result.scalars().all()

    _WATCHED_PCT = 90

    # Агрегируем по card_id: last_watched + max percent по каждому item (эпизоду)
    card_agg: dict[str, dict] = {}
    for tc in timecodes:
        if not _CARD_ID_RE.match(tc.card_id):
            continue
        try:
            pct = json.loads(tc.data).get("percent", 0)
        except Exception:
            pct = 0
        if tc.card_id not in card_agg:
            card_agg[tc.card_id] = {"last_watched": tc.updated_at, "items": {}}
        agg = card_agg[tc.card_id]
        # Для каждого item (эпизода) храним максимальный процент
        agg["items"][tc.item] = max(agg["items"].get(tc.item, 0), pct)

    if not card_agg:
        return []

    mc_result = await db.execute(
        select(MediaCard).where(MediaCard.card_id.in_(list(card_agg.keys())))
    )
    media_cards = {mc.card_id: mc for mc in mc_result.scalars().all()}

    today_str = date.today().isoformat()
    history = []
    for card_id, agg in card_agg.items():
        mc = media_cards.get(card_id)
        m = _CARD_ID_RE.match(card_id)
        items = agg["items"]
        max_pct = max(items.values(), default=0)

        watched_episodes = total_episodes = None
        is_ongoing = False
        progress = max_pct

        if card_id.endswith("_tv") and mc and mc.seasons_json:
            try:
                seasons = json.loads(mc.seasons_json)
                last_ep_s = mc.last_ep_season or 0
                last_ep_e = mc.last_ep_number or 0
                total_aired = 0
                total_all = 0
                for s in seasons:
                    snum = s.get("season_number") or 0
                    if snum == 0:
                        continue  # пропускаем спешлы
                    ep_count = s.get("episode_count") or 0
                    total_all += ep_count
                    if last_ep_s > 0:
                        if snum < last_ep_s:
                            total_aired += ep_count
                        elif snum == last_ep_s:
                            total_aired += last_ep_e
                    else:
                        # Нет данных о последней серии — используем дату сезона
                        s_air = s.get("air_date") or ""
                        if s_air and s_air <= today_str:
                            total_aired += ep_count

                watched_episodes = sum(1 for p in items.values() if p >= _WATCHED_PCT)
                total_episodes = total_aired

                # Онгоинг: next_ep_air_date если данные свежие, иначе старая логика
                if mc.next_ep_air_date is not None:
                    is_ongoing = bool(mc.next_ep_air_date) or bool(mc.last_air_date and mc.last_air_date > today_str)
                else:
                    is_ongoing = (total_all > total_aired) or bool(mc.last_air_date and mc.last_air_date > today_str)
                progress = min(round(watched_episodes / total_aired * 100), 100) if total_aired > 0 else 0
            except Exception:
                pass

        is_complete = (
            (watched_episodes is not None and total_episodes is not None
             and watched_episodes >= total_episodes > 0 and not is_ongoing)
            if card_id.endswith("_tv")
            else progress >= _WATCHED_PCT
        )

        entry = {
            "card_id": card_id,
            "tmdb_id": mc.tmdb_id if mc else (int(m.group(1)) if m else None),
            "media_type": mc.media_type if mc else (m.group(2) if m else None),
            "title": mc.title if mc else None,
            "original_title": mc.original_title if mc else None,
            "poster_path": mc.poster_path if mc else None,
            "year": mc.year if mc else None,
            "last_watched": agg["last_watched"].isoformat() if agg["last_watched"] else None,
            "max_percent": max_pct,
            "progress": progress,
            "watched_episodes": watched_episodes if (watched_episodes is not None and total_episodes is not None and watched_episodes < total_episodes) else None,
            "total_episodes": total_episodes if (watched_episodes is not None and total_episodes is not None and watched_episodes < total_episodes) else None,
            "is_complete": is_complete,
            "is_ongoing": is_ongoing,
            "last_ep_season": mc.last_ep_season if mc else None,
            "last_ep_number": mc.last_ep_number if mc else None,
        }
        history.append(entry)

    history.sort(key=lambda x: x["last_watched"] or "", reverse=True)

    total = len(history)
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(page, total_pages)
    start = (page - 1) * limit

    return {
        "results": history[start : start + limit],
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Управление профилями через device token (для плагина np_profiles.js)
# ---------------------------------------------------------------------------

@router.get("/profiles")
async def list_profiles(
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает список профилей устройства (LampaProfile) + количество таймкодов.
    Аутентификация: ?token=KEY или ?apikey=KEY
    """
    _require_device(device)

    lp_result = await db.execute(
        select(LampaProfile).where(LampaProfile.device_id == device.id)
        .order_by(LampaProfile.id.asc())
    )
    profiles = lp_result.scalars().all()

    result = []
    for lp in profiles:
        cnt = (await db.execute(
            select(func.count()).select_from(Timecode).where(
                Timecode.device_id == device.id,
                Timecode.lampa_profile_id == lp.lampa_profile_id,
            )
        )).scalar() or 0
        result.append({
            "profile_id": lp.lampa_profile_id,
            "name": lp.name,
            "icon": lp.icon,
            "timecodes_count": cnt,
        })

    user_role = await _get_user_role(device, db)
    limit = settings_cache.get_role_limit(user_role, "profile_limit")
    return {"profiles": result, "limit": limit}


class _CreateProfileBody(BaseModel):
    name: str
    profile_id: str | None = None  # если не указан — генерируется
    icon: str | None = None         # e.g. "id1"


@router.post("/profiles")
async def create_profile(
    body: _CreateProfileBody,
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Создаёт LampaProfile для устройства.
    Аутентификация: ?token=KEY или ?apikey=KEY
    """
    _require_device(device)

    name = body.name.strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail="Название профиля не может быть пустым")

    user_role = await _get_user_role(device, db)
    limit = settings_cache.get_role_limit(user_role, "profile_limit")

    count = (await db.execute(
        select(func.count()).select_from(LampaProfile)
        .where(LampaProfile.device_id == device.id)
    )).scalar() or 0

    if limit is not None and count >= limit:
        raise HTTPException(status_code=403, detail=f"Достигнут лимит профилей ({limit})")

    profile_id = (body.profile_id or "").strip().lstrip("_")[:100] or secrets.token_hex(4)

    existing = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device.id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Профиль с таким ID уже существует")

    icon = (body.icon or "").strip()[:20] or None

    # Если есть LampaProfile с пустым ID (создан авто через put_favorite без профиля) —
    # просто переименовываем его вместо создания нового + удаления старого.
    empty_lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device.id,
            LampaProfile.lampa_profile_id == "",
        )
    )).scalar_one_or_none()

    if empty_lp:
        empty_lp.lampa_profile_id = profile_id
        empty_lp.name = name
        if icon:
            empty_lp.icon = icon
        lp = empty_lp
    else:
        lp = LampaProfile(device_id=device.id, lampa_profile_id=profile_id, name=name, icon=icon)
        db.add(lp)

    # Переносим таймкоды с пустым profile_id (сохранены без профиля)
    await db.execute(
        update(Timecode)
        .where(Timecode.device_id == device.id, Timecode.lampa_profile_id == "")
        .values(lampa_profile_id=profile_id)
    )

    await db.commit()
    await db.refresh(lp)

    return {"ok": True, "profile_id": profile_id, "name": name, "icon": lp.icon}


class _RenameProfileBody(BaseModel):
    name: str | None = None
    icon: str | None = None


@router.patch("/profiles/{profile_id}")
async def rename_profile(
    profile_id: str,
    body: _RenameProfileBody,
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """Обновляет имя и/или иконку LampaProfile. Аутентификация: ?token=KEY"""
    _require_device(device)

    if body.name is None and body.icon is None:
        raise HTTPException(status_code=400, detail="Нужно передать name или icon")

    lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device.id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    if body.name is not None:
        name = body.name.strip()[:100]
        if not name:
            raise HTTPException(status_code=400, detail="Название не может быть пустым")
        lp.name = name

    if body.icon is not None:
        lp.icon = body.icon.strip()[:20] or None

    await db.commit()

    asyncio.create_task(ws_manager.broadcast(
        device.user_id, None,
        {"type": "profile_updated", "profile_id": profile_id, "name": lp.name, "icon": lp.icon},
    ))

    return {"ok": True, "profile_id": profile_id, "name": lp.name, "icon": lp.icon}


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: str,
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Удаляет LampaProfile и все его таймкоды.
    Аутентификация: ?token=KEY или ?apikey=KEY
    """
    _require_device(device)

    lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device.id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    await db.execute(delete(Timecode).where(
        Timecode.device_id == device.id,
        Timecode.lampa_profile_id == profile_id,
    ))
    await db.delete(lp)
    await db.commit()
    return {"ok": True}


@router.get("/favorite")
async def get_favorite(
    profile_id: str = Query(default=""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает сохранённые закладки. ?token=KEY&profile_id=ID (profile_id опционален)"""
    _require_device(device)

    lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device.id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()

    # Auto-rebuild: если history пуст — строим из MediaCards по существующим таймкодам.
    # Срабатывает один раз после первого импорта, когда фоновые задачи уже заполнили MediaCards.
    existing_fav = {}
    if lp and lp.favorite:
        try:
            existing_fav = json.loads(lp.favorite)
        except Exception:
            pass

    if not existing_fav.get("history"):
        card_ids = (await db.execute(
            select(Timecode.card_id).distinct().where(
                Timecode.device_id == device.id,
                Timecode.lampa_profile_id == profile_id,
                Timecode.card_id != "lampa_import",
            )
        )).scalars().all()
        valid_ids = [cid for cid in card_ids if _CARD_ID_RE.match(cid)]
        if valid_ids:
            mc_result = await db.execute(select(MediaCard).where(MediaCard.card_id.in_(valid_ids)))
            entries = [_media_card_to_entry(mc) for mc in mc_result.scalars().all()]
            if entries:
                role = await _get_user_role(device, db)
                await _merge_favorite_history(db, device.id, profile_id, entries, role)
                await db.commit()
                lp = (await db.execute(
                    select(LampaProfile).where(
                        LampaProfile.device_id == device.id,
                        LampaProfile.lampa_profile_id == profile_id,
                    )
                )).scalar_one_or_none()

    return {"favorite": json.loads(lp.favorite) if (lp and lp.favorite) else None}


class _FavoriteBody(BaseModel):
    favorite: Any


_FAV_CATEGORIES = ("like", "wath", "book", "history", "look", "viewed", "scheduled", "continued", "thrown")


def _trim_favorite(fav: dict, limit: int) -> dict:
    """Обрезает каждую категорию favorite до limit записей (новые идут первыми).
    Затем чистит 'card' — оставляет только карточки, чьи id есть хотя бы в одной категории.
    """
    fav = dict(fav)
    for cat in _FAV_CATEGORIES:
        if cat in fav and isinstance(fav[cat], list) and len(fav[cat]) > limit:
            fav[cat] = fav[cat][:limit]

    # Собираем актуальный набор id
    allowed_ids: set = set()
    for cat in _FAV_CATEGORIES:
        if isinstance(fav.get(cat), list):
            for item in fav[cat]:
                allowed_ids.add(item)

    if isinstance(fav.get("card"), list):
        fav["card"] = [c for c in fav["card"] if isinstance(c, dict) and c.get("id") in allowed_ids]

    return fav


@router.put("/favorite")
async def put_favorite(
    body: _FavoriteBody,
    profile_id: str = Query(default=""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """Сохраняет закладки. ?token=KEY&profile_id=ID (profile_id опционален)"""
    _require_device(device)

    favorite = body.favorite

    # Применяем лимит по роли пользователя
    if isinstance(favorite, dict):
        role = await _get_user_role(device, db)
        limit = settings_cache.get_role_limit(role, "favorite_limit")
        if limit is not None:
            favorite = _trim_favorite(favorite, limit)

    lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device.id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()
    if not lp:
        # Auto-create: LampaProfile создаётся при первом сохранении (как таймкоды)
        lp = LampaProfile(device_id=device.id, lampa_profile_id=profile_id, name="")
        db.add(lp)
        await db.flush()

    lp.favorite = json.dumps(favorite, ensure_ascii=False) if favorite is not None else None
    await db.commit()

    # Рассылаем обновление закладок другим соединениям пользователя
    asyncio.create_task(ws_manager.broadcast(
        device.user_id, None,
        {"type": "favorite", "profile_id": profile_id, "favorite": favorite},
    ))

    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket — real-time push таймкодов на другие устройства пользователя
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def ws_timecode(
    websocket: WebSocket,
    device: Device = Depends(get_device_by_token),
):
    """
    WebSocket для получения обновлений таймкодов от других устройств пользователя в реальном времени.
    Подключение: ws://BASE_URL/timecode/ws?token=KEY
    Сообщения: {"type": "timecode", "profile_id": "", "card_id": "123_movie", "item": "hash", "data": "..."}
    """
    if not device:
        await websocket.close(code=4001)
        return

    conn_id = await ws_manager.connect(device.user_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # держим соединение, ping/pong
    except WebSocketDisconnect:
        ws_manager.disconnect(device.user_id, conn_id)
    except Exception:
        ws_manager.disconnect(device.user_id, conn_id)

