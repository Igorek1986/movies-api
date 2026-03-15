import asyncio
from datetime import datetime, timedelta, timezone, date

from fastapi import Depends, Request, Response, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import User, Device, Session
from app import settings_cache

def _is_fully_blocked(user: User, now: datetime) -> bool:
    """Полная блокировка: blocked_at задан и premium уже истёк (или его нет)."""
    if not user.blocked_at:
        return False
    premium_until = user.premium_until
    if premium_until:
        if premium_until.tzinfo is None:
            premium_until = premium_until.replace(tzinfo=timezone.utc)
        return premium_until <= now
    return True

# ─── Inactive tracking ────────────────────────────────────────────────────────
# In-memory set of user_ids already updated today — prevents redundant DB writes
# when the same user sends multiple concurrent requests.
_active_today: set[int] = set()
_active_date: date = date.today()


def _should_update_active(user_id: int) -> bool:
    """Returns True (and marks) if this user_id hasn't been recorded today yet."""
    global _active_today, _active_date
    today = date.today()
    if _active_date != today:
        _active_today = set()
        _active_date = today
    if user_id in _active_today:
        return False
    _active_today.add(user_id)
    return True


async def _update_last_active(user_id: int) -> None:
    from app.db.database import async_session_maker
    today = date.today()
    async with async_session_maker() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user and user.last_active_at != today:
            user.last_active_at = today
            user.inactive_warned = False
            await db.commit()


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

    # Скользящее окно: продлеваем сессию если осталось меньше N дней до истечения
    ttl_days    = settings_cache.get_int("session_ttl_days")
    renew_days  = settings_cache.get_int("session_renew_days")
    if session.expires_at - now < timedelta(days=renew_days):
        session.expires_at = now + timedelta(days=ttl_days)
        await db.commit()
        response.set_cookie(
            key="session_key", value=key,
            httponly=True, max_age=ttl_days * 86400, samesite="lax",
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
    device = result.scalar_one_or_none()
    if not device:
        return None

    user = await db.get(User, device.user_id)
    if user and _is_fully_blocked(user, datetime.now(timezone.utc)):
        return None

    if _should_update_active(device.user_id):
        asyncio.create_task(_update_last_active(device.user_id))
    return device
