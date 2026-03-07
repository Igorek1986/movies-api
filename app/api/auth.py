import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db.database import get_db
import string
from app.db.models import User, PasswordResetToken, TelegramUser
from app.api.devices import _devices_with_stats, DEVICE_LIMITS
from app.utils import hash_password, verify_password, generate_api_key, validate_password, validate_name
from app.api.dependencies import get_current_user
from app.config import get_settings
from app import rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")
settings = get_settings()

COOKIE_NAME = "session_key"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней
RESET_CODE_TTL_MINUTES = 15


def _set_session_cookie(response, session_key: str):
    response.set_cookie(
        key=COOKIE_NAME, value=session_key,
        httponly=True, max_age=COOKIE_MAX_AGE, samesite="lax",
    )


async def _profiles_ctx(request, user, db, **extra) -> dict:
    """Контекст для шаблона profiles.html."""
    from app.db.models import TelegramUser
    devices = await _devices_with_stats(user.id, db)
    tg_result = await db.execute(
        select(TelegramUser).where(TelegramUser.user_id == user.id)
    )
    tg = tg_result.scalar_one_or_none()
    return {
        "request": request,
        "user": user,
        "profiles": devices,
        "device_limit": DEVICE_LIMITS.get(user.role, 3),
        "tg_linked": tg is not None,
        "tg_username": tg.username if (tg and tg.username) else None,
        **extra,
    }


# ─── Login ────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    reset_ok = request.query_params.get("reset") == "1"
    return templates.TemplateResponse("login.html", {
        "request": request,
        "success": "Пароль изменён. Войдите с новым паролем." if reset_ok else None,
    })


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host

    if not rate_limit.check_login(ip):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Слишком много попыток. Подождите 15 минут.",
        })

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Неверное имя пользователя или пароль",
        })

    rate_limit.clear_login(ip)
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, user.session_key)
    return response


# ─── Logout ───────────────────────────────────────────────────────────────────

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key=COOKIE_NAME)
    return response


# ─── Register ─────────────────────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    website: str = Form(""),       # Honeypot — bots fill this, humans don't
    db: AsyncSession = Depends(get_db),
):
    def _err(msg):
        return templates.TemplateResponse("register.html", {"request": request, "error": msg})

    # Honeypot check
    if website:
        return _err("Регистрация не разрешена")

    # Rate limit
    if not rate_limit.check_register(request.client.host):
        return _err("Слишком много регистраций с этого IP. Попробуйте позже.")

    is_valid, error_msg = validate_name(username)
    if not is_valid:
        return _err(error_msg)

    if password != password_confirm:
        return _err("Пароли не совпадают")

    is_valid, error_msg = validate_password(password)
    if not is_valid:
        return _err(error_msg)

    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        return _err("Имя пользователя уже занято")

    session_key = generate_api_key()
    user = User(username=username, password_hash=hash_password(password), session_key=session_key)
    db.add(user)
    await db.commit()
    logger.info(f"New user registered: {username}")

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, session_key)
    return response


# ─── Profile redirect (legacy) ────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, current_user: User = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return RedirectResponse(url="/profiles", status_code=302)


# ─── Change password ──────────────────────────────────────────────────────────

@router.post("/profile/reset-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    async def _err(msg):
        return templates.TemplateResponse(
            "profiles.html", await _profiles_ctx(request, current_user, db, error=msg)
        )

    if not verify_password(current_password, current_user.password_hash):
        return await _err("Неверный текущий пароль")

    if verify_password(new_password, current_user.password_hash):
        return await _err("Новый пароль не должен совпадать с текущим")

    if new_password != new_password_confirm:
        return await _err("Новые пароли не совпадают")

    is_valid, error_msg = validate_password(new_password)
    if not is_valid:
        return await _err(error_msg)

    current_user.password_hash = hash_password(new_password)
    await db.commit()
    logger.info(f"Password changed: {current_user.username}")

    return templates.TemplateResponse(
        "profiles.html",
        await _profiles_ctx(request, current_user, db, success="Пароль успешно изменён"),
    )


# ─── Delete account ───────────────────────────────────────────────────────────

@router.post("/profile/delete")
async def delete_account(
    request: Request,
    password: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    if not verify_password(password, current_user.password_hash):
        return templates.TemplateResponse(
            "profiles.html",
            await _profiles_ctx(request, current_user, db, error="Неверный пароль"),
        )

    await db.delete(current_user)
    await db.commit()

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=COOKIE_NAME)
    return response


# ─── Forgot password ──────────────────────────────────────────────────────────

def _forgot_ctx(request, **extra):
    return {"request": request, "bot_name": settings.TELEGRAM_BOT_NAME, **extra}


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", _forgot_ctx(request))


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    username: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not rate_limit.check_forgot(request.client.host):
        return templates.TemplateResponse("forgot_password.html", _forgot_ctx(
            request, error="Слишком много запросов. Попробуйте через час."
        ))

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user:
        tg_result = await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == user.id)
        )
        tg_user = tg_result.scalar_one_or_none()

        if tg_user:
            # Генерируем 6-значный код
            await db.execute(
                delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
            )
            code = "".join(secrets.choice(string.digits) for _ in range(6))
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=RESET_CODE_TTL_MINUTES)
            db.add(PasswordResetToken(user_id=user.id, token=code, expires_at=expires_at))
            await db.commit()

            from app.bot import send_reset_code
            ok = await send_reset_code(tg_user.telegram_id, user.username, code)
            if not ok:
                logger.error(f"Failed to send reset code to telegram_id={tg_user.telegram_id}")
        else:
            logger.warning(f"Reset requested for {user.username}: no Telegram linked")

    # Одно сообщение — не раскрываем наличие аккаунта
    return templates.TemplateResponse("forgot_password.html", _forgot_ctx(
        request,
        step=2,
        username=username,
        success="Если аккаунт существует и Telegram привязан — код отправлен в бот.",
    ))


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password_submit(
    request: Request,
    username: str = Form(...),
    code: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    def _err(msg):
        return templates.TemplateResponse("forgot_password.html", _forgot_ctx(
            request, step=2, username=username, error=msg
        ))

    code = code.strip()
    now = datetime.now(timezone.utc)

    user_result = await db.execute(select(User).where(User.username == username))
    user = user_result.scalar_one_or_none()
    if not user:
        return _err("Неверный код или имя пользователя")

    token_result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.token == code,
            PasswordResetToken.expires_at > now,
        )
    )
    token_obj = token_result.scalar_one_or_none()
    if not token_obj:
        return _err("Неверный или истёкший код")

    if new_password != new_password_confirm:
        return _err("Пароли не совпадают")

    is_valid, error_msg = validate_password(new_password)
    if not is_valid:
        return _err(error_msg)

    user.password_hash = hash_password(new_password)
    await db.delete(token_obj)
    await db.commit()

    logger.info(f"Password reset via Telegram for user: {user.username}")
    return RedirectResponse(url="/login?reset=1", status_code=status.HTTP_302_FOUND)
