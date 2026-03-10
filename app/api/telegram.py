"""
Роуты для работы с Telegram-ботом:
  POST /telegram/generate-link-code  — сгенерировать код привязки (авторизованный пользователь)
  POST /telegram/unlink              — отвязать Telegram
  GET  /telegram/status              — статус привязки (JSON)
  POST /bot/webhook                  — входящие обновления от Telegram
"""

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db.database import get_db
from app.db.models import TelegramUser, TelegramLinkCode, User
from app.api.dependencies import get_current_user
from app.constants import TELEGRAM_LINK_CODE_TTL_MINUTES as LINK_CODE_TTL_MINUTES

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Генерация кода привязки ──────────────────────────────────────────────────

@router.post("/telegram/generate-link-code")
async def generate_link_code(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    # Удаляем старые коды этого пользователя
    await db.execute(
        delete(TelegramLinkCode).where(TelegramLinkCode.user_id == current_user.id)
    )

    # Генерируем уникальный 6-значный код
    for _ in range(10):
        code = "".join(secrets.choice(string.digits) for _ in range(6))
        existing = await db.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.code == code)
        )
        if not existing.scalar_one_or_none():
            break
    else:
        raise HTTPException(status_code=503, detail="Не удалось сгенерировать код")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    db.add(TelegramLinkCode(user_id=current_user.id, code=code, expires_at=expires_at))
    await db.commit()

    from app.config import get_settings
    return {
        "code": code,
        "expires_in": LINK_CODE_TTL_MINUTES * 60,
        "bot_name": get_settings().TELEGRAM_BOT_NAME,
    }


# ─── Статус привязки ──────────────────────────────────────────────────────────

@router.get("/telegram/status")
async def telegram_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.user_id == current_user.id)
    )
    tg = result.scalar_one_or_none()

    if not tg:
        return {"linked": False}

    return {
        "linked": True,
        "username": tg.username,
        "linked_at": tg.linked_at.isoformat() if tg.linked_at else None,
    }


# ─── Отвязать Telegram ────────────────────────────────────────────────────────

@router.post("/telegram/unlink")
async def telegram_unlink(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    await db.execute(
        delete(TelegramUser).where(TelegramUser.user_id == current_user.id)
    )
    await db.commit()
    return RedirectResponse(url="/profiles", status_code=302)


# ─── Webhook ──────────────────────────────────────────────────────────────────

@router.post("/bot/webhook")
async def bot_webhook(request: Request, background_tasks: BackgroundTasks):
    logger.debug(f"📨 Webhook request from {request.client.host}")
    from app.bot import get_bot, get_dp
    from aiogram.types import Update
    from app.config import get_settings

    bot = get_bot()
    dp = get_dp()
    if not bot or not dp:
        return {"ok": True}

    # Проверяем secret_token если webhook настроен с ним
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    settings = get_settings()
    expected = settings.TELEGRAM_BOT_TOKEN.split(":")[1] if settings.TELEGRAM_BOT_TOKEN else None
    if expected and secret_token != expected:
        return {"ok": True}

    try:
        data = await request.json()
        update = Update.model_validate(data)
    except Exception:
        return {"ok": True}
    # Отвечаем Telegram немедленно, обрабатываем в фоне
    background_tasks.add_task(dp.feed_update, bot, update)
    return {"ok": True}
