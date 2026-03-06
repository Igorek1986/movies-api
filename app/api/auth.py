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
from app.db.models import User, PasswordResetToken
from app.api.devices import _devices_with_stats, DEVICE_LIMITS
from app.utils import hash_password, verify_password, generate_api_key, validate_password, validate_name
from app.api.dependencies import get_current_user
from app.config import get_settings
from app import rate_limit
from app.email_utils import send_password_reset, is_configured as smtp_ok

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")
settings = get_settings()

COOKIE_NAME = "session_key"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней
RESET_TOKEN_TTL_HOURS = 1
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _set_session_cookie(response, session_key: str):
    response.set_cookie(
        key=COOKIE_NAME, value=session_key,
        httponly=True, max_age=COOKIE_MAX_AGE, samesite="lax",
    )


async def _profiles_ctx(request, user, db, **extra) -> dict:
    """Контекст для шаблона profiles.html."""
    devices = await _devices_with_stats(user.id, db)
    return {
        "request": request,
        "user": user,
        "profiles": devices,
        "device_limit": DEVICE_LIMITS.get(user.role, 3),
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
    response = RedirectResponse(url="/profiles", status_code=status.HTTP_302_FOUND)
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

    response = RedirectResponse(url="/profiles?registered=1", status_code=status.HTTP_302_FOUND)
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


# ─── Update email ─────────────────────────────────────────────────────────────

@router.post("/profile/update-email")
async def update_email(
    request: Request,
    email: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    async def _err(msg):
        return templates.TemplateResponse(
            "profiles.html", await _profiles_ctx(request, current_user, db, error=msg)
        )

    email = email.strip().lower() or None

    if email:
        if not EMAIL_RE.match(email):
            return await _err("Некорректный формат email")
        dup = await db.execute(
            select(User).where(User.email == email, User.id != current_user.id)
        )
        if dup.scalar_one_or_none():
            return await _err("Этот email уже используется")

    current_user.email = email
    await db.commit()

    return templates.TemplateResponse(
        "profiles.html",
        await _profiles_ctx(
            request, current_user, db,
            success="Email сохранён" if email else "Email удалён",
        ),
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

@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    username: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # Rate limit
    if not rate_limit.check_forgot(request.client.host):
        return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "error": "Слишком много запросов. Попробуйте через час.",
        })

    # Always show the same success message (don't reveal if user exists)
    generic_ok = "Если аккаунт существует и email указан — письмо отправлено."

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user and user.email:
        # Remove old tokens for this user
        await db.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )

        token_str = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS)
        db.add(PasswordResetToken(user_id=user.id, token=token_str, expires_at=expires_at))
        await db.commit()

        reset_url = f"{settings.BASE_URL}/reset-password?token={token_str}"

        if smtp_ok(settings):
            try:
                await send_password_reset(settings, user.email, user.username, reset_url)
            except Exception as e:
                logger.error(f"Failed to send reset email to {user.email}: {e}")
        else:
            # SMTP not configured — log reset link for development
            logger.warning(f"SMTP not configured. Reset link for {user.username}: {reset_url}")

    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "success": generic_ok,
    })


# ─── Reset password (via token) ───────────────────────────────────────────────

@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request, token: str, db: AsyncSession = Depends(get_db)
):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token == token,
            PasswordResetToken.expires_at > now,
        )
    )
    if not result.scalar_one_or_none():
        return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "error": "Ссылка недействительна или истекла. Запросите новую.",
        })
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    def _form_err(msg):
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token, "error": msg,
        })

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token == token,
            PasswordResetToken.expires_at > now,
        )
    )
    token_obj = result.scalar_one_or_none()
    if not token_obj:
        return _form_err("Ссылка недействительна или истекла.")

    if new_password != new_password_confirm:
        return _form_err("Пароли не совпадают")

    is_valid, error_msg = validate_password(new_password)
    if not is_valid:
        return _form_err(error_msg)

    user_result = await db.execute(select(User).where(User.id == token_obj.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        return _form_err("Пользователь не найден")

    user.password_hash = hash_password(new_password)
    await db.delete(token_obj)
    await db.commit()

    logger.info(f"Password reset via email for user: {user.username}")
    return RedirectResponse(url="/login?reset=1", status_code=status.HTTP_302_FOUND)
