from datetime import datetime, timezone

from fastapi import Depends, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import User, Device, Session


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    """
    Авторизация в веб-интерфейсе через cookie (session_key → Session.key).
    Возвращает None если не авторизован или сессия истекла.
    """
    key = request.cookies.get("session_key")
    if not key:
        return None

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Session).where(Session.key == key, Session.expires_at > now)
    )
    session = result.scalar_one_or_none()
    if not session:
        return None

    return await db.get(User, session.user_id)


async def get_device_by_token(
    token: str = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Device | None:
    """
    Авторизация API-запросов (Lampa) по token из query параметра.
    Используется для эндпоинтов /timecode и /{category}.
    """
    if not token:
        return None

    result = await db.execute(select(Device).where(Device.token == token))
    return result.scalar_one_or_none()
