from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request, Response, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import User, Device, Session
from app.constants import SESSION_TTL_DAYS, SESSION_RENEW_DAYS

SESSION_TTL = timedelta(days=SESSION_TTL_DAYS)
SESSION_RENEW_BEFORE = timedelta(days=SESSION_RENEW_DAYS)
COOKIE_MAX_AGE = SESSION_TTL_DAYS * 86400


async def get_current_user(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> User | None:
    """
    Авторизация в веб-интерфейсе через cookie (session_key → Session.key).
    Скользящее окно: продлеваем сессию при активности (если < 15 дней до истечения).
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

    # Скользящее окно: продлеваем сессию если осталось меньше половины TTL
    if session.expires_at - now < SESSION_RENEW_BEFORE:
        session.expires_at = now + SESSION_TTL
        await db.commit()
        response.set_cookie(
            key="session_key", value=key,
            httponly=True, max_age=COOKIE_MAX_AGE, samesite="lax",
        )

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
