"""
Таймкоды — прогресс просмотра, привязанный к профилю.

Форматы данных:
  Внутренний (БД):
    Timecode(profile_id, card_id, item, data)
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
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select, delete

from app.config import get_settings
from app.db.database import get_db, async_session_maker
from app.db.models import Profile, Timecode, MediaCard
from app.api.dependencies import get_profile_by_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/timecode", tags=["timecodes"])

WATCHED_THRESHOLD = 90  # процент для пометки «просмотрено»


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _require_profile(profile: Profile | None) -> Profile:
    if not profile:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий apikey")
    return profile


async def _upsert_timecodes(db: AsyncSession, profile_id: int, rows: list[dict]):
    """UPSERT списка таймкодов. rows: [{card_id, item, data}]"""
    if not rows:
        return 0

    # Дедупликация: последний побеждает
    unique: dict[tuple, dict] = {}
    for r in rows:
        unique[(r["card_id"], r["item"])] = r

    values = [
        {"profile_id": profile_id, "card_id": r["card_id"], "item": r["item"], "data": r["data"]}
        for r in unique.values()
    ]

    stmt = pg_insert(Timecode).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Timecode.profile_id, Timecode.card_id, Timecode.item],
        set_={"data": stmt.excluded.data, "updated_at": stmt.excluded.updated_at},
    )
    await db.execute(stmt)
    await db.commit()
    return len(values)


# ---------------------------------------------------------------------------
# Загрузка таймкодов профиля в память (используется для фильтрации в main.py)
# ---------------------------------------------------------------------------

async def load_profile_timecodes(db: AsyncSession, profile_id: int) -> dict[str, dict[str, str]]:
    """
    Возвращает словарь {card_id: {item: data_json_string}}.
    Тот же формат что у Lampac /timecode/all_views.
    """
    result = await db.execute(
        select(Timecode).where(Timecode.profile_id == profile_id)
    )
    rows = result.scalars().all()

    out: dict[str, dict[str, str]] = {}
    for tc in rows:
        out.setdefault(tc.card_id, {})[tc.item] = tc.data
    return out


def get_watched_movie_ids(
    timecodes: dict[str, dict[str, str]],
    threshold: int = WATCHED_THRESHOLD,
) -> set[str]:
    """Возвращает card_id фильмов, где хоть один таймкод >= threshold."""
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


async def _fetch_and_store_media_card(card_id: str, tmdb_id: int, media_type: str) -> None:
    """Фоновая задача: получает метаданные из TMDB и сохраняет/обновляет в media_cards."""
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
            )
            if resp.status_code != 200:
                return
            data = resp.json()

        date = data.get(date_key) or ""
        values: dict = {
            "card_id": card_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": data.get(title_key) or "",
            "original_title": data.get(orig_key) or "",
            "poster_path": data.get("poster_path") or "",
            "year": date[:4],
            "backdrop_path": data.get("backdrop_path") or "",
            "overview": data.get("overview") or "",
            "vote_average": data.get("vote_average"),
            "release_date": date,
        }
        if media_type == "tv":
            seasons = data.get("seasons")
            values["last_air_date"] = data.get("last_air_date") or ""
            values["number_of_seasons"] = data.get("number_of_seasons")
            values["seasons_json"] = json.dumps(seasons, ensure_ascii=False) if seasons else None

        async with async_session_maker() as db:
            mc_stmt = pg_insert(MediaCard).values([values])
            mc_stmt = mc_stmt.on_conflict_do_update(
                index_elements=["card_id"],
                set_={k: mc_stmt.excluded[k] for k in values if k != "card_id"},
            )
            await db.execute(mc_stmt)
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
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Плагин отправляет прогресс просмотра при выходе из плеера.
    Body: {card_id, item, data}  где data — JSON-строка {time, duration, percent}.
    """
    _require_profile(profile)

    # Валидируем data как JSON
    try:
        json.loads(data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="data должна быть JSON-строкой")

    await _upsert_timecodes(db, profile.id, [{"card_id": card_id, "item": item, "data": data}])
    logger.debug(f"Timecode saved: profile={profile.id}, card={card_id}, item={item}")

    # Фоново подтягиваем метаданные из TMDB если ещё нет
    m = _CARD_ID_RE.match(card_id)
    if m:
        asyncio.create_task(_fetch_and_store_media_card(card_id, int(m.group(1)), m.group(2)))

    return {"success": True}


# ---------------------------------------------------------------------------
# Пакетный импорт (из Lampac или при первоначальной загрузке)
# ---------------------------------------------------------------------------

@router.post("/batch")
async def batch_save_timecodes(
    timecodes: list[dict] = Body(...),
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Пакетный UPSERT таймкодов.
    Body: [{card_id, item, data}, ...]
    """
    _require_profile(profile)

    rows = []
    for tc in timecodes:
        if not tc.get("card_id") or not tc.get("item") or not tc.get("data"):
            continue
        rows.append({"card_id": tc["card_id"], "item": tc["item"], "data": tc["data"]})

    saved = await _upsert_timecodes(db, profile.id, rows)
    return {"success": True, "saved": saved}


# ---------------------------------------------------------------------------
# Экспорт — формат совместим с Lampac /timecode/all_views
# ---------------------------------------------------------------------------

@router.get("/export")
async def export_timecodes(
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Экспорт всех таймкодов профиля.
    Формат: {card_id: {item: data_json_string}}
    Совместим с Lampac /timecode/all_views для использования в других клиентах.
    """
    _require_profile(profile)
    timecodes = await load_profile_timecodes(db, profile.id)
    return timecodes


# ---------------------------------------------------------------------------
# Импорт из Lampac (формат all_views)
# ---------------------------------------------------------------------------

@router.post("/import/lampac")
async def import_from_lampac(
    data: dict[str, dict[str, str]] = Body(...),
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт из Lampac /timecode/all_views.
    Body: {"123_movie": {"hash1": '{"percent":100,...}'}, ...}
    """
    _require_profile(profile)

    rows = []
    for card_id, items in data.items():
        for item, tc_data in items.items():
            rows.append({"card_id": card_id, "item": item, "data": tc_data})

    saved = await _upsert_timecodes(db, profile.id, rows)
    logger.info(f"Lampac import: profile={profile.id}, saved={saved}")
    return {"success": True, "saved": saved}


# ---------------------------------------------------------------------------
# Импорт из Lampa localStorage (ключ file_view)
# ---------------------------------------------------------------------------

@router.post("/import/lampa")
async def import_from_lampa(
    data: dict[str, Any] = Body(...),
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт из Lampa localStorage['file_view'].
    Body: {"572566331": {"duration": 6450, "time": 2715, "percent": 42, "profile": 0}, ...}

    В Lampa формате нет card_id — хранится с card_id="lampa_import".
    Эти таймкоды попадают в историю просмотров, но не участвуют в серверной фильтрации.
    Для фильтрации используйте синхронизацию MyShows.
    """
    _require_profile(profile)

    rows = []
    for item_hash, tc_data in data.items():
        if not isinstance(tc_data, dict):
            continue
        # Нормализуем в наш формат data-строки
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

    saved = await _upsert_timecodes(db, profile.id, rows)
    logger.info(f"Lampa import: profile={profile.id}, saved={saved}")
    return {
        "success": True,
        "saved": saved,
        "note": "Импортировано без card_id. Для серверной фильтрации используйте MyShows sync.",
    }


# ---------------------------------------------------------------------------
# Удаление таймкода
# ---------------------------------------------------------------------------

@router.delete("")
async def delete_timecode(
    card_id: str = Query(...),
    item: str = Query(...),
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Удалить конкретный таймкод (например, пометить как непросмотренное)."""
    _require_profile(profile)

    await db.execute(
        delete(Timecode).where(
            Timecode.profile_id == profile.id,
            Timecode.card_id == card_id,
            Timecode.item == item,
        )
    )
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# История просмотра профиля
# ---------------------------------------------------------------------------

@router.get("/history")
async def get_watch_history(
    profile: Profile = Depends(get_profile_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    История просмотра профиля.
    Возвращает список карточек (по одной на card_id) с метаданными из TMDB,
    отсортированных по дате последнего просмотра (новые первые).
    Исключает card_id без tmdb_id (lampa_import и аналоги).
    """
    _require_profile(profile)

    # Загружаем все таймкоды профиля
    result = await db.execute(
        select(Timecode)
        .where(Timecode.profile_id == profile.id)
        .order_by(Timecode.updated_at.desc())
    )
    timecodes = result.scalars().all()

    # Группируем по card_id: макс. процент и последнее время
    card_agg: dict[str, dict] = {}
    for tc in timecodes:
        if not _CARD_ID_RE.match(tc.card_id):
            continue  # пропускаем lampa_import и другие без tmdb_id
        try:
            pct = json.loads(tc.data).get("percent", 0)
        except Exception:
            pct = 0
        if tc.card_id not in card_agg:
            card_agg[tc.card_id] = {"last_watched": tc.updated_at, "max_percent": pct}
        else:
            if pct > card_agg[tc.card_id]["max_percent"]:
                card_agg[tc.card_id]["max_percent"] = pct

    if not card_agg:
        return []

    # Загружаем MediaCard для всех найденных card_id
    mc_result = await db.execute(
        select(MediaCard).where(MediaCard.card_id.in_(list(card_agg.keys())))
    )
    media_cards = {mc.card_id: mc for mc in mc_result.scalars().all()}

    history = []
    for card_id, agg in card_agg.items():
        mc = media_cards.get(card_id)
        m = _CARD_ID_RE.match(card_id)
        entry = {
            "card_id": card_id,
            "tmdb_id": mc.tmdb_id if mc else (int(m.group(1)) if m else None),
            "media_type": mc.media_type if mc else (m.group(2) if m else None),
            "title": mc.title if mc else None,
            "original_title": mc.original_title if mc else None,
            "poster_path": mc.poster_path if mc else None,
            "year": mc.year if mc else None,
            "last_watched": agg["last_watched"].isoformat() if agg["last_watched"] else None,
            "max_percent": agg["max_percent"],
        }
        history.append(entry)

    history.sort(key=lambda x: x["last_watched"] or "", reverse=True)
    return history
