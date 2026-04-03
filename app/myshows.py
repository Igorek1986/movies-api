import asyncio
import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import stats
from app.api.dependencies import get_device_by_token
from app.config import get_settings
from app.db.database import get_db
from app.db.models import Device, MediaCard, MyShowsItem, MyShowsUserStatus, MyShowsWatching
from app.tmdb_utils import ensure_media_cards_multi

MYSHOWS_AUTH_URL = get_settings().MYSHOWS_AUTH_URL
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/myshows", tags=["myshows"])

PAGE_SIZE = 20


# ── Pydantic ────────────────────────────────────────────────────────────────

class StatusItem(BaseModel):
    myshows_id: int
    tmdb_id: int
    media_type: str = "tv"            # tv / movie
    cache_type: Optional[str] = None  # watching/watchlist/watched/cancelled/remove
    unwatched_count: Optional[int] = None
    next_episode: Optional[str] = None
    progress_marker: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _build_card(status, item: MyShowsItem, mc: MediaCard, cache_type: str) -> dict:
    card: dict = {
        "id": mc.tmdb_id,
        "media_type": mc.media_type,
        "myshows_id": item.myshows_id,
        "poster_path": mc.poster_path or "",
        "backdrop_path": mc.backdrop_path or "",
        "overview": mc.overview or "",
        "vote_average": mc.vote_average or 0,
    }
    if mc.media_type == "tv":
        card["name"] = mc.title or ""
        card["original_name"] = mc.original_title or ""
        card["first_air_date"] = mc.release_date or ""
        card["number_of_seasons"] = mc.number_of_seasons or 0
    else:
        card["title"] = mc.title or ""
        card["original_title"] = mc.original_title or ""
        card["release_date"] = mc.release_date or ""

    if cache_type == "watching":
        card["unwatched_count"] = status.unwatched_count or 0
        card["next_episode"] = status.next_episode
        card["progress_marker"] = status.progress_marker

    return card


async def _upsert_item_map(items: List[StatusItem], db: AsyncSession) -> dict:
    """Upsert MyShowsItem, вернуть маппинг myshows_id → item.id."""
    item_values = [
        {"myshows_id": i.myshows_id, "tmdb_id": i.tmdb_id, "media_type": i.media_type}
        for i in items
    ]
    await db.execute(
        pg_insert(MyShowsItem).values(item_values).on_conflict_do_update(
            index_elements=["myshows_id"],
            set_={
                "tmdb_id": pg_insert(MyShowsItem).excluded.tmdb_id,
                "media_type": pg_insert(MyShowsItem).excluded.media_type,
            },
        )
    )
    await db.flush()

    myshows_ids = [i.myshows_id for i in items]
    rows = (
        await db.execute(
            select(MyShowsItem.id, MyShowsItem.myshows_id).where(
                MyShowsItem.myshows_id.in_(myshows_ids)
            )
        )
    ).fetchall()
    return {row.myshows_id: row.id for row in rows}


async def _upsert_watching(
    device_id: int,
    profile_id: str,
    items: List[StatusItem],
    db: AsyncSession,
) -> None:
    """Синхронизирует таблицу myshows_watching. Пишет только fetchFromMyShowsAPI."""
    if not items:
        await db.execute(
            delete(MyShowsWatching).where(
                MyShowsWatching.device_id == device_id,
                MyShowsWatching.lampa_profile_id == profile_id,
            )
        )
        await db.commit()
        return

    id_map = await _upsert_item_map(items, db)

    status_values = [
        {
            "device_id": device_id,
            "lampa_profile_id": profile_id,
            "item_id": id_map[i.myshows_id],
            "unwatched_count": i.unwatched_count,
            "next_episode": i.next_episode,
            "progress_marker": i.progress_marker,
        }
        for i in items
        if i.myshows_id in id_map
    ]
    await db.execute(
        pg_insert(MyShowsWatching).values(status_values).on_conflict_do_update(
            index_elements=["device_id", "lampa_profile_id", "item_id"],
            set_={
                "unwatched_count": pg_insert(MyShowsWatching).excluded.unwatched_count,
                "next_episode": pg_insert(MyShowsWatching).excluded.next_episode,
                "progress_marker": pg_insert(MyShowsWatching).excluded.progress_marker,
                "updated_at": func.now(),
            },
        )
    )

    # Dual write: watching → История (watched). Не перезаписывает watchlist/cancelled.
    history_values = [
        {"device_id": device_id, "lampa_profile_id": profile_id,
         "item_id": id_map[i.myshows_id], "cache_type": "watched"}
        for i in items if i.myshows_id in id_map
    ]
    await db.execute(
        pg_insert(MyShowsUserStatus).values(history_values).on_conflict_do_update(
            index_elements=["device_id", "lampa_profile_id", "item_id"],
            set_={"cache_type": "watched", "updated_at": func.now()},
        )
    )

    # Удаляем устаревшие записи
    incoming_item_ids = list(id_map.values())
    if incoming_item_ids:
        await db.execute(
            delete(MyShowsWatching).where(
                MyShowsWatching.device_id == device_id,
                MyShowsWatching.lampa_profile_id == profile_id,
                MyShowsWatching.item_id.notin_(incoming_item_ids),
            )
        )

    await ensure_media_cards_multi([(i.tmdb_id, i.media_type) for i in items])
    await db.commit()


async def _upsert_status(
    device_id: int,
    profile_id: str,
    cache_type: str,
    items: List[StatusItem],
    db: AsyncSession,
) -> None:
    """Синхронизирует myshows_user_status для watchlist/watched/cancelled."""
    if not items:
        await db.execute(
            delete(MyShowsUserStatus).where(
                MyShowsUserStatus.device_id == device_id,
                MyShowsUserStatus.lampa_profile_id == profile_id,
                MyShowsUserStatus.cache_type == cache_type,
            )
        )
        await db.commit()
        return

    id_map = await _upsert_item_map(items, db)

    status_values = [
        {
            "device_id": device_id,
            "lampa_profile_id": profile_id,
            "item_id": id_map[i.myshows_id],
            "cache_type": cache_type,
        }
        for i in items
        if i.myshows_id in id_map
    ]
    await db.execute(
        pg_insert(MyShowsUserStatus).values(status_values).on_conflict_do_update(
            index_elements=["device_id", "lampa_profile_id", "item_id"],
            set_={
                "cache_type": pg_insert(MyShowsUserStatus).excluded.cache_type,
                "updated_at": func.now(),
            },
        )
    )

    # Удаляем устаревшие записи этого типа
    incoming_item_ids = list(id_map.values())
    if incoming_item_ids:
        await db.execute(
            delete(MyShowsUserStatus).where(
                MyShowsUserStatus.device_id == device_id,
                MyShowsUserStatus.lampa_profile_id == profile_id,
                MyShowsUserStatus.cache_type == cache_type,
                MyShowsUserStatus.item_id.notin_(incoming_item_ids),
            )
        )

    await ensure_media_cards_multi([(i.tmdb_id, i.media_type) for i in items])
    await db.commit()


async def _get_watching_page(
    device_id: int,
    profile_id: str,
    db: AsyncSession,
) -> dict:
    """Возвращает все записи myshows_watching без пагинации."""
    base_filter = [
        MyShowsWatching.device_id == device_id,
        MyShowsWatching.lampa_profile_id == profile_id,
    ]

    total = (
        await db.execute(
            select(func.count())
            .select_from(MyShowsWatching)
            .join(MyShowsItem, MyShowsItem.id == MyShowsWatching.item_id)
            .join(
                MediaCard,
                (MediaCard.tmdb_id == MyShowsItem.tmdb_id)
                & (MediaCard.media_type == MyShowsItem.media_type),
            )
            .where(*base_filter)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(MyShowsWatching, MyShowsItem, MediaCard)
            .join(MyShowsItem, MyShowsItem.id == MyShowsWatching.item_id)
            .join(
                MediaCard,
                (MediaCard.tmdb_id == MyShowsItem.tmdb_id)
                & (MediaCard.media_type == MyShowsItem.media_type),
            )
            .where(*base_filter)
            .order_by(MyShowsWatching.updated_at.desc())
        )
    ).all()

    results = [_build_card(w, it, mc, "watching") for w, it, mc in rows]
    return {"results": results, "page": 1, "total_pages": 1, "total_results": total}


async def _get_status_page(
    device_id: int,
    profile_id: str,
    cache_type: str,
    page: int,
    db: AsyncSession,
) -> dict:
    """Возвращает страницу myshows_user_status для watchlist/watched/cancelled."""
    base_filter = [
        MyShowsUserStatus.device_id == device_id,
        MyShowsUserStatus.lampa_profile_id == profile_id,
        MyShowsUserStatus.cache_type == cache_type,
    ]

    total = (
        await db.execute(
            select(func.count())
            .select_from(MyShowsUserStatus)
            .join(MyShowsItem, MyShowsItem.id == MyShowsUserStatus.item_id)
            .join(
                MediaCard,
                (MediaCard.tmdb_id == MyShowsItem.tmdb_id)
                & (MediaCard.media_type == MyShowsItem.media_type),
            )
            .where(*base_filter)
        )
    ).scalar_one()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    rows = (
        await db.execute(
            select(MyShowsUserStatus, MyShowsItem, MediaCard)
            .join(MyShowsItem, MyShowsItem.id == MyShowsUserStatus.item_id)
            .join(
                MediaCard,
                (MediaCard.tmdb_id == MyShowsItem.tmdb_id)
                & (MediaCard.media_type == MyShowsItem.media_type),
            )
            .where(*base_filter)
            .order_by(MyShowsUserStatus.updated_at.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()

    results = [_build_card(s, it, mc, cache_type) for s, it, mc in rows]
    return {
        "results": results,
        "page": page,
        "total_pages": total_pages,
        "total_results": total,
    }


# ── Auth proxy ───────────────────────────────────────────────────────────────

@router.post("/auth")
async def proxy_auth(request: Request):
    try:
        data = await request.json()
        login = data.get("login")
        password = data.get("password")

        if not login or not password:
            raise HTTPException(status_code=400, detail="Login and password are required")

        logger.info(f"Received auth request for login: {login}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                MYSHOWS_AUTH_URL,
                json={"login": login, "password": password},
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="MyShows authentication failed",
                )

            auth_data = response.json()
            token = auth_data.get("token")
            refresh_token = auth_data.get("refreshToken")
            token_v3 = response.cookies.get("msAuthToken")

            if not token:
                raise HTTPException(status_code=500, detail="No token received from MyShows")

            logger.info(f"Successfully authenticated user: {login}")
            stats.track_myshows_user(login)

            return {"token": token, "token_v3": token_v3, "refreshToken": refresh_token}

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Set single item status ──────────────────────────────────────────────────

@router.post("/set_status")
async def set_status(
    item: StatusItem,
    profile_id: str = Query(""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """Upsert статуса одного элемента. cache_type='remove' удаляет запись."""
    if not item.cache_type:
        raise HTTPException(status_code=400, detail="cache_type required")

    # Upsert MyShowsItem (глобальный маппинг)
    await db.execute(
        pg_insert(MyShowsItem)
        .values([{"myshows_id": item.myshows_id, "tmdb_id": item.tmdb_id, "media_type": item.media_type}])
        .on_conflict_do_update(
            index_elements=["myshows_id"],
            set_={"tmdb_id": pg_insert(MyShowsItem).excluded.tmdb_id,
                  "media_type": pg_insert(MyShowsItem).excluded.media_type},
        )
    )
    await db.flush()

    ms_item = (await db.execute(
        select(MyShowsItem).where(MyShowsItem.myshows_id == item.myshows_id)
    )).scalar_one()

    if item.cache_type == "remove":
        await db.execute(
            delete(MyShowsWatching).where(
                MyShowsWatching.device_id == device.id,
                MyShowsWatching.lampa_profile_id == profile_id,
                MyShowsWatching.item_id == ms_item.id,
            )
        )
        await db.execute(
            delete(MyShowsUserStatus).where(
                MyShowsUserStatus.device_id == device.id,
                MyShowsUserStatus.lampa_profile_id == profile_id,
                MyShowsUserStatus.item_id == ms_item.id,
            )
        )
    elif item.cache_type == "watching":
        # watching пишем в отдельную таблицу (без unwatched_count — fetchFromMyShowsAPI обновит)
        await db.execute(
            pg_insert(MyShowsWatching)
            .values([{
                "device_id": device.id,
                "lampa_profile_id": profile_id,
                "item_id": ms_item.id,
                "unwatched_count": item.unwatched_count,
                "next_episode": item.next_episode,
                "progress_marker": item.progress_marker,
            }])
            .on_conflict_do_update(
                index_elements=["device_id", "lampa_profile_id", "item_id"],
                set_={
                    "unwatched_count": pg_insert(MyShowsWatching).excluded.unwatched_count,
                    "next_episode": pg_insert(MyShowsWatching).excluded.next_episode,
                    "progress_marker": pg_insert(MyShowsWatching).excluded.progress_marker,
                    "updated_at": func.now(),
                },
            )
        )
        # Dual write: watching → История (watched)
        await db.execute(
            pg_insert(MyShowsUserStatus)
            .values([{
                "device_id": device.id,
                "lampa_profile_id": profile_id,
                "item_id": ms_item.id,
                "cache_type": "watched",
            }])
            .on_conflict_do_update(
                index_elements=["device_id", "lampa_profile_id", "item_id"],
                set_={"cache_type": "watched", "updated_at": func.now()},
            )
        )
        asyncio.create_task(ensure_media_cards_multi([(item.tmdb_id, item.media_type)]))
    else:
        await db.execute(
            pg_insert(MyShowsUserStatus)
            .values([{
                "device_id": device.id,
                "lampa_profile_id": profile_id,
                "item_id": ms_item.id,
                "cache_type": item.cache_type,
            }])
            .on_conflict_do_update(
                index_elements=["device_id", "lampa_profile_id", "item_id"],
                set_={
                    "cache_type": pg_insert(MyShowsUserStatus).excluded.cache_type,
                    "updated_at": func.now(),
                },
            )
        )
        # Если перешёл в другой статус — убираем из watching
        await db.execute(
            delete(MyShowsWatching).where(
                MyShowsWatching.device_id == device.id,
                MyShowsWatching.lampa_profile_id == profile_id,
                MyShowsWatching.item_id == ms_item.id,
            )
        )
        asyncio.create_task(ensure_media_cards_multi([(item.tmdb_id, item.media_type)]))

    await db.commit()
    return {"ok": True}


@router.get("/status")
async def get_status(
    tmdb_id: int = Query(...),
    media_type: str = Query(...),
    profile_id: str = Query(""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает cache_type для конкретного элемента по tmdb_id."""
    # Сначала проверяем watching
    watching = (await db.execute(
        select(MyShowsWatching.item_id)
        .join(MyShowsItem, MyShowsItem.id == MyShowsWatching.item_id)
        .where(
            MyShowsItem.tmdb_id == tmdb_id,
            MyShowsItem.media_type == media_type,
            MyShowsWatching.device_id == device.id,
            MyShowsWatching.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()

    if watching is not None:
        return {"cache_type": "watching"}

    row = (await db.execute(
        select(MyShowsUserStatus.cache_type)
        .join(MyShowsItem, MyShowsItem.id == MyShowsUserStatus.item_id)
        .where(
            MyShowsItem.tmdb_id == tmdb_id,
            MyShowsItem.media_type == media_type,
            MyShowsUserStatus.device_id == device.id,
            MyShowsUserStatus.lampa_profile_id == profile_id,
        )
    )).scalar_one_or_none()
    return {"cache_type": row}


# ── Watching (непросмотренные сериалы) ──────────────────────────────────────

@router.post("/watching")
async def watching_post(
    items: List[StatusItem],
    profile_id: str = Query(""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    await _upsert_watching(device.id, profile_id, items, db)
    return await _get_watching_page(device.id, profile_id, db)


@router.get("/watching")
async def watching_get(
    profile_id: str = Query(""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    return await _get_watching_page(device.id, profile_id, db)


# ── Watchlist / Cancelled (с пагинацией) ────────────────────────────────────

def _make_category_endpoints(cache_type: str):
    path = f"/{cache_type}"

    @router.post(path, name=f"myshows_{cache_type}_post")
    async def category_post(
        items: List[StatusItem],
        page: int = Query(1, ge=1),
        profile_id: str = Query(""),
        device: Device = Depends(get_device_by_token),
        db: AsyncSession = Depends(get_db),
    ):
        await _upsert_status(device.id, profile_id, cache_type, items, db)
        return await _get_status_page(device.id, profile_id, cache_type, page, db)

    @router.get(path, name=f"myshows_{cache_type}_get")
    async def category_get(
        page: int = Query(1, ge=1),
        profile_id: str = Query(""),
        device: Device = Depends(get_device_by_token),
        db: AsyncSession = Depends(get_db),
    ):
        return await _get_status_page(device.id, profile_id, cache_type, page, db)


for _ct in ("watchlist", "cancelled"):
    _make_category_endpoints(_ct)


# ── Watched = История (только watched статус) ────────────────────────────────

@router.post("/watched", name="myshows_watched_post")
async def watched_post(
    items: List[StatusItem],
    page: int = Query(1, ge=1),
    profile_id: str = Query(""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    await _upsert_status(device.id, profile_id, "watched", items, db)
    return await _get_status_page(device.id, profile_id, "watched", page, db)


@router.get("/watched", name="myshows_watched_get")
async def watched_get(
    page: int = Query(1, ge=1),
    profile_id: str = Query(""),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    return await _get_status_page(device.id, profile_id, "watched", page, db)
