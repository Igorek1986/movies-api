import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templates import get_templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db.database import get_db
import string
import json as _json
from app.db.models import User, PasswordResetToken, TelegramUser, Totp2faPending, Session
from app.api.devices import _devices_with_stats, _import_ctx
from app.utils import (
    hash_password, verify_password, generate_api_key, validate_password, validate_name,
    generate_totp_secret, get_totp_uri, verify_totp, make_totp_qr_base64,
    generate_backup_codes, verify_backup_code, backup_codes_count, get_real_ip,
)
from app.api.dependencies import get_current_user
from app.config import get_settings
from app import rate_limit, settings_cache

logger = logging.getLogger(__name__)
router = APIRouter()
templates = get_templates()
settings = get_settings()

COOKIE_NAME = "session_key"
PENDING_2FA_COOKIE = "2fa_pending"


async def _notify_new_session(user_id: int, ip: str, ua: str, base_url: str):
    """Fire-and-forget: Telegram-уведомление о новом входе."""
    try:
        from app.db.database import async_session_maker
        from app.bot import send_new_session_notification
        from app.utils import parse_user_agent
        async with async_session_maker() as db:
            result = await db.execute(select(TelegramUser).where(TelegramUser.user_id == user_id))
            tg = result.scalar_one_or_none()
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
        if tg and user and user.notifications_enabled:
            change_password_url = f"{base_url}/profiles"
            await send_new_session_notification(tg.telegram_id, ip, parse_user_agent(ua), change_password_url, user.username)
    except Exception as e:
        logger.warning(f"Session notification failed for user {user_id}: {e}")


async def _create_session(db: AsyncSession, user_id: int, request: Request) -> str:
    """Создаёт запись Session и возвращает ключ для cookie."""
    key = generate_api_key()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings_cache.get_int("session_ttl_days"))
    ip = get_real_ip(request)
    ua = request.headers.get("User-Agent", "")[:500]
    db.add(Session(user_id=user_id, key=key, expires_at=expires_at, ip=ip, user_agent=ua))
    await db.commit()
    base_url = str(request.base_url).rstrip("/")
    asyncio.create_task(_notify_new_session(user_id, ip, ua, base_url))
    from app.api.dependencies import _should_update_active, _update_last_active
    if _should_update_active(user_id):
        asyncio.create_task(_update_last_active(user_id))
    return key


async def _delete_all_sessions(db: AsyncSession, user_id: int):
    """Удаляет все сессии пользователя."""
    await db.execute(delete(Session).where(Session.user_id == user_id))
    await db.commit()


def _set_session_cookie(response, session_key: str):
    response.set_cookie(
        key=COOKIE_NAME, value=session_key,
        httponly=True, max_age=settings_cache.get_int("session_ttl_days") * 86400, samesite="lax",
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
        "device_limit": settings_cache.get_role_limit(user.role, "device_limit"),
        "tg_linked": tg is not None,
        "tg_username": tg.username if (tg and tg.username) else None,
        "totp_enabled": user.totp_enabled,
        "backup_codes_count": backup_codes_count(user.backup_codes),
        "notifications_enabled": user.notifications_enabled is not False,
        "notify_start": user.notify_start if user.notify_start is not None else 9,
        "notify_end":   user.notify_end   if user.notify_end   is not None else 22,
        "user_timezone": user.timezone or "",
        **_import_ctx(user),
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
    ip = get_real_ip(request)

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

    # Если включена 2FA — перенаправляем на страницу проверки кода
    if user.totp_enabled:
        pending_token = generate_api_key()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings_cache.get_int("pending_2fa_ttl_sec"))
        db.add(Totp2faPending(user_id=user.id, token=pending_token, expires_at=expires_at))
        await db.commit()
        response = RedirectResponse(url="/verify-2fa", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key=PENDING_2FA_COOKIE, value=pending_token,
            httponly=True, max_age=settings_cache.get_int("pending_2fa_ttl_sec"), samesite="lax",
        )
        return response

    session_key = await _create_session(db, user.id, request)
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, session_key)
    return response


# ─── Logout ───────────────────────────────────────────────────────────────────

@router.get("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    key = request.cookies.get(COOKIE_NAME)
    if key:
        await db.execute(delete(Session).where(Session.key == key))
        await db.commit()
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
    if not rate_limit.check_register(get_real_ip(request)):
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

    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(f"New user registered: {username}")

    session_key = await _create_session(db, user.id, request)
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
    totp_code: str = Form(""),
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

    if current_user.totp_enabled:
        if not verify_totp(current_user.totp_secret, totp_code.strip()):
            return await _err("Неверный код 2FA")

    if verify_password(new_password, current_user.password_hash):
        return await _err("Новый пароль не должен совпадать с текущим")

    if new_password != new_password_confirm:
        return await _err("Новые пароли не совпадают")

    is_valid, error_msg = validate_password(new_password)
    if not is_valid:
        return await _err(error_msg)

    current_user.password_hash = hash_password(new_password)
    await db.commit()
    await _delete_all_sessions(db, current_user.id)
    logger.info(f"Password changed: {current_user.username}")

    response = RedirectResponse(url="/login?reset=1", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key=COOKIE_NAME)
    return response


# ─── Delete account ───────────────────────────────────────────────────────────

@router.post("/profile/delete")
async def delete_account(
    request: Request,
    password: str = Form(...),
    totp_code: str = Form(""),
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

    if current_user.totp_enabled and not verify_totp(current_user.totp_secret, totp_code.strip()):
        return templates.TemplateResponse(
            "profiles.html",
            await _profiles_ctx(request, current_user, db, error="Неверный код 2FA"),
        )

    await db.delete(current_user)
    await db.commit()

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=COOKIE_NAME)
    return response


# ─── Notification settings ────────────────────────────────────────────────────

VALID_TIMEZONES = {
    "Europe/Moscow", "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Europe/Kiev", "Europe/Minsk", "Europe/Istanbul", "Asia/Yekaterinburg",
    "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
    "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka", "Asia/Almaty",
    "Asia/Tashkent", "Asia/Dubai", "Asia/Baku", "Asia/Tbilisi",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
}


@router.post("/profile/notifications")
async def save_notifications(
    request: Request,
    notifications_enabled: bool = Form(False),
    notify_start: int = Form(9),
    notify_end: int = Form(22),
    user_timezone: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    notify_start = max(0, min(23, notify_start))
    notify_end   = max(0, min(23, notify_end))
    if notify_start >= notify_end:
        return templates.TemplateResponse(
            "profiles.html",
            await _profiles_ctx(request, current_user, db, error="Начало окна уведомлений должно быть раньше конца"),
        )

    tz = user_timezone.strip() if user_timezone.strip() in VALID_TIMEZONES else None

    current_user.notifications_enabled = notifications_enabled
    current_user.notify_start = notify_start
    current_user.notify_end   = notify_end
    current_user.timezone     = tz
    await db.commit()

    return templates.TemplateResponse(
        "profiles.html",
        await _profiles_ctx(request, current_user, db, success="Настройки уведомлений сохранены"),
    )


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
    if not rate_limit.check_forgot(get_real_ip(request)):
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
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings_cache.get_int("reset_code_ttl_minutes"))
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
    if user.totp_enabled:
        user.totp_enabled = False
        user.totp_secret = None
        user.backup_codes = None
        logger.info(f"2FA disabled during password reset for user: {user.username}")
    await db.delete(token_obj)
    await db.commit()

    logger.info(f"Password reset via Telegram for user: {user.username}")
    return RedirectResponse(url="/login?reset=1", status_code=status.HTTP_302_FOUND)


# ─── 2FA Verify (при входе) ───────────────────────────────────────────────────

@router.get("/verify-2fa", response_class=HTMLResponse)
async def verify_2fa_page(request: Request, db: AsyncSession = Depends(get_db)):
    pending_token = request.cookies.get(PENDING_2FA_COOKIE)
    if not pending_token:
        return RedirectResponse("/login", status_code=302)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Totp2faPending).where(
            Totp2faPending.token == pending_token,
            Totp2faPending.expires_at > now,
        )
    )
    if not result.scalar_one_or_none():
        response = RedirectResponse("/login?error=expired", status_code=302)
        response.delete_cookie(PENDING_2FA_COOKIE)
        return response
    return templates.TemplateResponse("verify_2fa.html", {"request": request})


@router.post("/verify-2fa", response_class=HTMLResponse)
async def verify_2fa_submit(
    request: Request,
    code: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = get_real_ip(request)

    if not rate_limit.check_2fa(ip):
        return templates.TemplateResponse("verify_2fa.html", {
            "request": request,
            "error": "Слишком много попыток. Подождите 15 минут.",
        })

    pending_token = request.cookies.get(PENDING_2FA_COOKIE)
    if not pending_token:
        return RedirectResponse("/login", status_code=302)

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Totp2faPending).where(
            Totp2faPending.token == pending_token,
            Totp2faPending.expires_at > now,
        )
    )
    pending = result.scalar_one_or_none()
    if not pending:
        response = RedirectResponse("/login?error=expired", status_code=302)
        response.delete_cookie(PENDING_2FA_COOKIE)
        return response

    user = await db.get(User, pending.user_id)
    code_clean = code.strip().replace(" ", "")
    verified = verify_totp(user.totp_secret, code_clean)

    if not verified and user.backup_codes:
        stored = _json.loads(user.backup_codes)
        matched, updated = verify_backup_code(code_clean, stored)
        if matched:
            user.backup_codes = _json.dumps(updated)
            verified = True

    if not verified:
        return templates.TemplateResponse("verify_2fa.html", {
            "request": request,
            "error": "Неверный код. Попробуйте ещё раз.",
        })

    await db.delete(pending)
    await db.commit()
    rate_limit.clear_2fa(ip)

    session_key = await _create_session(db, user.id, request)
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(PENDING_2FA_COOKIE)
    _set_session_cookie(response, session_key)
    return response


# ─── 2FA Setup ────────────────────────────────────────────────────────────────

@router.get("/profile/2fa/setup", response_class=HTMLResponse)
async def setup_2fa_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)
    if current_user.totp_enabled:
        return RedirectResponse("/profiles", status_code=302)

    # Генерируем (или переиспользуем уже сгенерированный) секрет
    if not current_user.totp_secret:
        current_user.totp_secret = generate_totp_secret()
        await db.commit()
        await db.refresh(current_user)

    uri = get_totp_uri(current_user.totp_secret, current_user.username)
    qr = make_totp_qr_base64(uri)
    return templates.TemplateResponse("setup_2fa.html", {
        "request": request,
        "secret": current_user.totp_secret,
        "qr_data_uri": qr,
    })


@router.post("/profile/2fa/enable", response_class=HTMLResponse)
async def enable_2fa(
    request: Request,
    code: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    if not current_user.totp_secret or not verify_totp(current_user.totp_secret, code.strip()):
        uri = get_totp_uri(current_user.totp_secret or "", current_user.username)
        qr = make_totp_qr_base64(uri)
        return templates.TemplateResponse("setup_2fa.html", {
            "request": request,
            "secret": current_user.totp_secret,
            "qr_data_uri": qr,
            "error": "Неверный код. Проверьте время на устройстве и попробуйте ещё раз.",
        })

    plain_codes, hashed_codes = generate_backup_codes()
    current_user.totp_enabled = True
    current_user.backup_codes = _json.dumps(hashed_codes)
    await db.commit()
    logger.info(f"2FA enabled for user: {current_user.username}")

    return templates.TemplateResponse("backup_codes.html", {
        "request": request,
        "codes": plain_codes,
    })


@router.post("/profile/2fa/disable", response_class=HTMLResponse)
async def disable_2fa(
    request: Request,
    password: str = Form(""),
    totp_code: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    async def _err(msg):
        return templates.TemplateResponse(
            "profiles.html", await _profiles_ctx(request, current_user, db, error=msg)
        )

    if not verify_password(password, current_user.password_hash):
        return await _err("Неверный пароль")

    totp_ok = current_user.totp_secret and verify_totp(current_user.totp_secret, totp_code.strip())
    if not totp_ok:
        return await _err("Неверный код из приложения 2FA")

    current_user.totp_enabled = False
    current_user.totp_secret = None
    current_user.backup_codes = None
    await db.commit()
    logger.info(f"2FA disabled for user: {current_user.username}")

    return templates.TemplateResponse(
        "profiles.html",
        await _profiles_ctx(request, current_user, db, success="Двухфакторная аутентификация отключена"),
    )
