import hashlib
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import get_settings
from app.db.database import get_db
from app.db.models import User, Device, Timecode, USER_ROLES
from app.api.dependencies import get_current_user
from app import rate_limit, settings_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="templates")

_COOKIE = "admin_session"


def _session_token(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _check_admin_cookie(request: Request) -> bool:
    settings = get_settings()
    if not settings.ADMIN_PASSWORD:
        return False
    return request.cookies.get(_COOKIE) == _session_token(settings.ADMIN_PASSWORD)


async def _get_admin_user(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Возвращает текущего пользователя если у него is_admin=True, иначе None."""
    user = await get_current_user(request, response, db)
    return user if (user and user.is_admin) else None


async def _check_admin(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> bool:
    """Доступ разрешён если: валидный ADMIN_PASSWORD cookie ИЛИ пользователь с is_admin=True."""
    if _check_admin_cookie(request):
        return True
    return await _get_admin_user(request, response, db) is not None


# ---------------------------------------------------------------------------
# Login (для доступа по паролю без учётной записи)
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # Если уже авторизован — сразу в панель
    if await _check_admin(request, response, db):
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse("admin_login.html", {"request": request})


@router.post("/login")
async def admin_login(request: Request, password: str = Form(...)):
    settings = get_settings()
    if not settings.ADMIN_PASSWORD or password != settings.ADMIN_PASSWORD:
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Неверный пароль"},
            status_code=401,
        )
    response = RedirectResponse(url="/admin", status_code=302)
    response.set_cookie(_COOKIE, _session_token(password), httponly=True, samesite="lax", max_age=7 * 86400)
    return response


@router.get("/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Dashboard — список пользователей
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    authed = await _check_admin(request, response, db)
    if not authed:
        return RedirectResponse(url="/admin/login", status_code=302)

    # Пользователь для unified header (может быть None если зашли по паролю)
    current_user = await _get_admin_user(request, response, db)

    result = await db.execute(select(User).order_by(User.id))
    users = result.scalars().all()

    users_data = []
    for u in users:
        cnt_result = await db.execute(
            select(func.count()).select_from(Device).where(Device.user_id == u.id)
        )
        device_count = cnt_result.scalar() or 0
        dlimit = settings_cache.get_role_limit(u.role, "device_limit")
        users_data.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "is_admin": u.is_admin,
            "device_count": device_count,
            "device_limit": dlimit if dlimit is not None else "∞",
            "premium_until": u.premium_until,
            "timecode_grace_until": u.timecode_grace_until,
            "created_at": u.created_at,
        })

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "user": current_user,
        "users": users_data,
        "roles": USER_ROLES,
        "success": request.query_params.get("success"),
    })


# ---------------------------------------------------------------------------
# Смена роли пользователя
# ---------------------------------------------------------------------------

@router.post("/user/{user_id}/role")
async def change_user_role(
    request: Request,
    response: Response,
    user_id: int,
    role: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not await _check_admin(request, response, db):
        raise HTTPException(status_code=403)

    if role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Неверная роль: {role}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    old_role = user.role
    user.role = role

    if role == "premium":
        duration_days = settings_cache.get_int("premium_duration_days")
        # Истекает в конец (23:59:59) дня через N дней — не зависит от времени выдачи
        expiry_day = (datetime.now(timezone.utc) + timedelta(days=duration_days)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        user.premium_until = expiry_day
        user.premium_warned = False  # сброс чтобы предупреждение отправилось при следующем сроке
        user.timecode_grace_until = None
    elif role in ("simple", "super"):
        user.premium_until = None

    # При понижении лимита таймкодов — применить ограничение
    if old_role != role:
        old_limit = settings_cache.get_role_limit(old_role, "timecode_limit")
        new_limit = settings_cache.get_role_limit(role,     "timecode_limit")
        # Запускаем только если новый лимит строго меньше старого (или был безлимитный → стал ограниченным)
        limit_reduced = new_limit is not None and (old_limit is None or new_limit < old_limit)
        if limit_reduced:
            dev_ids = (await db.execute(
                select(Device.id).where(Device.user_id == user.id)
            )).scalars().all()
            if dev_ids:
                tc_total = (await db.execute(
                    select(func.count()).select_from(Timecode)
                    .where(Timecode.device_id.in_(dev_ids))
                )).scalar() or 0
                if tc_total > new_limit:
                    grace_days = settings_cache.get_int("timecode_grace_days")
                    if grace_days == 0:
                        # Немедленная очистка
                        from app.tasks import _cleanup_timecodes
                        await db.commit()  # зафиксировать смену роли до очистки
                        await _cleanup_timecodes(db, user.id, new_limit, user.username)
                    else:
                        user.timecode_grace_until = datetime.now(timezone.utc) + timedelta(days=grace_days)

    await db.commit()

    logger.info(f"Admin: user {user.username} role changed {old_role} → {role}")
    return RedirectResponse(url="/admin", status_code=302)


@router.post("/user/{user_id}/extend-premium")
async def extend_premium(
    request: Request,
    response: Response,
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    if not await _check_admin(request, response, db):
        raise HTTPException(status_code=403)

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.role != "premium":
        raise HTTPException(status_code=400, detail="Пользователь не является Premium")

    now = datetime.now(timezone.utc)
    base = user.premium_until if (user.premium_until and user.premium_until > now) else now
    user.premium_until = (base + timedelta(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)
    user.premium_warned = False
    await db.commit()

    logger.info(f"Admin: extended premium for {user.username} until {user.premium_until.strftime('%d.%m.%Y')}")
    return RedirectResponse(url="/admin", status_code=302)


@router.post("/user/{user_id}/toggle-admin")
async def toggle_user_admin(
    request: Request,
    response: Response,
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    if not await _check_admin(request, response, db):
        raise HTTPException(status_code=403)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    user.is_admin = not user.is_admin
    await db.commit()

    logger.info(f"Admin: user {user.username} is_admin → {user.is_admin}")
    return RedirectResponse(url="/admin", status_code=302)


@router.post("/user/{user_id}/reset-import")
async def reset_user_import(
    request: Request,
    response: Response,
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    if not await _check_admin(request, response, db):
        raise HTTPException(status_code=403)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    rate_limit.reset_import(user_id)
    logger.info(f"Admin: import limit reset for user_id={user_id} ({user.username})")
    from urllib.parse import quote
    msg = quote(f"Лимит импорта сброшен для {user.username}")
    return RedirectResponse(url=f"/admin?success={msg}", status_code=302)


# ---------------------------------------------------------------------------
# Настройки приложения
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_page(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if not await _check_admin(request, response, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    current_user = await _get_admin_user(request, response, db)
    return templates.TemplateResponse("admin_settings.html", {
        "request": request,
        "user": current_user,
        "settings": settings_cache.all_settings(),
        "groups": settings_cache.GROUPS,
        "labels": settings_cache.LABELS,
        "success": request.query_params.get("success"),
    })


@router.post("/settings")
async def admin_settings_update(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if not await _check_admin(request, response, db):
        raise HTTPException(status_code=403)

    form = await request.form()
    allowed_keys = set(settings_cache.DEFAULTS.keys())

    for key, value in form.items():
        if key not in allowed_keys:
            continue
        value = str(value).strip()
        if value == "":
            continue
        await settings_cache.set_setting(key, value, db)

    logger.info("Admin: app settings updated")
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/admin/settings?success={quote('Настройки сохранены')}",
        status_code=302,
    )
