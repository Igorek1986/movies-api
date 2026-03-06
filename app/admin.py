import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import get_settings
from app.db.database import get_db
from app.db.models import User, Device, USER_ROLES, DEVICE_LIMITS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="templates")

_COOKIE = "admin_session"


def _session_token(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _check_admin(request: Request) -> bool:
    settings = get_settings()
    if not settings.ADMIN_PASSWORD:
        return False
    expected = _session_token(settings.ADMIN_PASSWORD)
    return request.cookies.get(_COOKIE) == expected


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
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
    response.set_cookie(_COOKIE, _session_token(password), httponly=True, samesite="lax")
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
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).order_by(User.id))
    users = result.scalars().all()

    # Считаем устройства для каждого пользователя
    users_data = []
    for u in users:
        cnt_result = await db.execute(
            select(func.count()).select_from(Device).where(Device.user_id == u.id)
        )
        device_count = cnt_result.scalar() or 0
        limit = DEVICE_LIMITS.get(u.role, 3)
        users_data.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "device_count": device_count,
            "device_limit": limit if limit is not None else "∞",
            "created_at": u.created_at,
        })

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "users": users_data,
        "roles": USER_ROLES,
        "device_limits": DEVICE_LIMITS,
    })


# ---------------------------------------------------------------------------
# Смена роли пользователя
# ---------------------------------------------------------------------------

@router.post("/user/{user_id}/role")
async def change_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not _check_admin(request):
        raise HTTPException(status_code=403)

    if role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Неверная роль: {role}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    old_role = user.role
    user.role = role
    await db.commit()

    logger.info(f"Admin: user {user.username} role changed {old_role} → {role}")
    return RedirectResponse(url="/admin", status_code=302)
