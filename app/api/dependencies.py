from datetime import datetime, timezone

from fastapi import Depends, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import User, Profile


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    """
    Авторизация в веб-интерфейсе через cookie (session_key).
    Возвращает None если не авторизован.
    """
    session_key = request.cookies.get("session_key")
    if not session_key:
        return None

    result = await db.execute(select(User).where(User.session_key == session_key))
    return result.scalar_one_or_none()


async def get_profile_by_api_key(
    apikey: str = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Profile | None:
    """
    Авторизация API-запросов (Lampa) по apikey из query параметра.
    Используется для эндпоинтов /timecode и /{category}.
    """
    if not apikey:
        return None

    result = await db.execute(select(Profile).where(Profile.api_key == apikey))
    return result.scalar_one_or_none()
