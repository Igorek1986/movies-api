"""
Утилиты для работы с TMDB: загрузка данных сериалов/фильмов и upsert в media_cards.
Используется из main.py и myshows.py.
"""
import asyncio
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.database import async_session_maker
from app.db.models import MediaCard

logger = logging.getLogger(__name__)
TMDB_TOKEN = get_settings().TMDB_TOKEN


async def fetch_and_save_tv(tmdb_id: int) -> None:
    """Загружает данные сериала из TMDB и сохраняет в media_cards."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                headers={"Authorization": TMDB_TOKEN},
                params={"language": "ru"},
            )
            if resp.status_code != 200:
                logger.warning(f"TMDB tv/{tmdb_id} вернул {resp.status_code}")
                return
            data = resp.json()

        date_val = data.get("first_air_date") or ""
        seasons = data.get("seasons")
        card_id = f"{tmdb_id}_tv"
        values = {
            "card_id": card_id,
            "tmdb_id": tmdb_id,
            "media_type": "tv",
            "title": data.get("name") or "",
            "original_title": data.get("original_name") or "",
            "poster_path": data.get("poster_path") or "",
            "year": date_val[:4],
            "backdrop_path": data.get("backdrop_path") or "",
            "overview": data.get("overview") or "",
            "vote_average": data.get("vote_average"),
            "release_date": date_val,
            "last_air_date": data.get("last_air_date") or "",
            "number_of_seasons": data.get("number_of_seasons"),
            "number_of_episodes": data.get("number_of_episodes"),
            "seasons_json": json.dumps(seasons, ensure_ascii=False) if seasons else None,
            "last_ep_season": (data.get("last_episode_to_air") or {}).get("season_number"),
            "last_ep_number": (data.get("last_episode_to_air") or {}).get("episode_number"),
            "next_ep_air_date": (data.get("next_episode_to_air") or {}).get("air_date") or "",
            "episode_run_time": ((data.get("episode_run_time") or [None])[0]),
        }

        async with async_session_maker() as db:
            stmt = pg_insert(MediaCard).values([values]).on_conflict_do_nothing(
                index_elements=["card_id"]
            )
            await db.execute(stmt)
            await db.commit()

    except Exception as e:
        logger.error(f"Ошибка загрузки TMDB tv/{tmdb_id}: {e}")


async def fetch_and_save_movie(tmdb_id: int) -> None:
    """Загружает данные фильма из TMDB и сохраняет в media_cards."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                headers={"Authorization": TMDB_TOKEN},
                params={"language": "ru"},
            )
            if resp.status_code != 200:
                logger.warning(f"TMDB movie/{tmdb_id} вернул {resp.status_code}")
                return
            data = resp.json()

        date_val = data.get("release_date") or ""
        card_id = f"{tmdb_id}_movie"
        values = {
            "card_id": card_id,
            "tmdb_id": tmdb_id,
            "media_type": "movie",
            "title": data.get("title") or "",
            "original_title": data.get("original_title") or "",
            "poster_path": data.get("poster_path") or "",
            "year": date_val[:4],
            "backdrop_path": data.get("backdrop_path") or "",
            "overview": data.get("overview") or "",
            "vote_average": data.get("vote_average"),
            "release_date": date_val,
            "runtime": data.get("runtime"),
        }

        async with async_session_maker() as db:
            stmt = pg_insert(MediaCard).values([values]).on_conflict_do_nothing(
                index_elements=["card_id"]
            )
            await db.execute(stmt)
            await db.commit()

    except Exception as e:
        logger.error(f"Ошибка загрузки TMDB movie/{tmdb_id}: {e}")


async def ensure_media_cards(tmdb_ids: list[int]) -> None:
    """Для tv tmdb_id которых нет в media_cards — загружает из TMDB параллельно."""
    if not tmdb_ids:
        return
    async with async_session_maker() as db:
        result = await db.execute(
            select(MediaCard.tmdb_id).where(
                MediaCard.tmdb_id.in_(tmdb_ids),
                MediaCard.media_type == "tv",
            )
        )
        existing = {row[0] for row in result.fetchall()}

    missing = [tid for tid in tmdb_ids if tid not in existing]
    if missing:
        logger.info(f"Загружаю {len(missing)} новых карточек из TMDB")
        await asyncio.gather(*[fetch_and_save_tv(tid) for tid in missing])


async def ensure_media_cards_multi(items: list[tuple[int, str]]) -> None:
    """Для списка (tmdb_id, media_type) загружает из TMDB те, которых нет в media_cards."""
    if not items:
        return

    tmdb_ids = [i[0] for i in items]
    async with async_session_maker() as db:
        result = await db.execute(
            select(MediaCard.tmdb_id, MediaCard.media_type).where(
                MediaCard.tmdb_id.in_(tmdb_ids)
            )
        )
        existing = {(row[0], row[1]) for row in result.fetchall()}

    missing = [(tid, mt) for tid, mt in items if (tid, mt) not in existing]
    if not missing:
        return

    logger.info(f"Загружаю {len(missing)} новых карточек из TMDB (tv+movie)")
    tasks = []
    for tmdb_id, media_type in missing:
        if media_type == "movie":
            tasks.append(fetch_and_save_movie(tmdb_id))
        else:
            tasks.append(fetch_and_save_tv(tmdb_id))
    await asyncio.gather(*tasks)
