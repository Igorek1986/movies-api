import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db.database import get_db
from app.db.models import Profile, DeviceCode, Timecode, User
from app.utils import generate_profile_api_key, generate_device_code, validate_name
from app.api.dependencies import get_current_user
from app.constants import DEVICE_CODE_TTL_MINUTES

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

async def _profiles_with_stats(user_id: int, db: AsyncSession) -> list[dict]:
    result = await db.execute(select(Profile).where(Profile.user_id == user_id))
    profiles = result.scalars().all()
    out = []
    for p in profiles:
        cnt = await db.execute(select(Timecode).where(Timecode.profile_id == p.id))
        out.append({
            "id": p.id, "name": p.name, "api_key": p.api_key,
            "created_at": p.created_at,
            "timecodes_count": len(cnt.scalars().all()),
        })
    return out


async def _get_profile_or_404(profile_id: int, user: User, db: AsyncSession) -> Profile:
    result = await db.execute(
        select(Profile).where(Profile.id == profile_id, Profile.user_id == user.id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    return profile


# ---------------------------------------------------------------------------
# Веб-страница управления профилями (встроена в /profile)
# ---------------------------------------------------------------------------

@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    profiles = await _profiles_with_stats(current_user.id, db)
    return templates.TemplateResponse("profiles.html", {
        "request": request,
        "user": current_user,
        "profiles": profiles,
    })


# ---------------------------------------------------------------------------
# CRUD профилей
# ---------------------------------------------------------------------------

@router.post("/profiles/create")
async def create_profile(
    request: Request,
    name: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    async def _err(msg):
        profiles = await _profiles_with_stats(current_user.id, db)
        return templates.TemplateResponse("profiles.html", {
            "request": request, "user": current_user, "profiles": profiles, "error": msg,
        }, status_code=400)

    name = name.strip()[:100]
    if not name:
        return await _err("Имя профиля не может быть пустым")
    is_valid, error_msg = validate_name(name)
    if not is_valid:
        return await _err(error_msg)

    api_key = generate_profile_api_key()

    profile = Profile(user_id=current_user.id, name=name, api_key=api_key)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    logger.info(f"Profile created: user={current_user.username}, name={name}, id={profile.id}")

    return templates.TemplateResponse("profile_key_once.html", {
        "request": request,
        "profile": profile,
        "api_key": api_key,
    })


@router.post("/profiles/{profile_id}/rename")
async def rename_profile(
    request: Request,
    profile_id: int,
    name: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    profile = await _get_profile_or_404(profile_id, current_user, db)
    new_name = name.strip()[:100]
    is_valid, error_msg = validate_name(new_name)
    if not is_valid:
        profiles = await _profiles_with_stats(current_user.id, db)
        return templates.TemplateResponse("profiles.html", {
            "request": request, "user": current_user, "profiles": profiles, "error": error_msg,
        }, status_code=400)
    profile.name = new_name
    await db.commit()
    return RedirectResponse(url="/profiles", status_code=302)


@router.post("/profiles/{profile_id}/regenerate")
async def regenerate_profile_key(
    request: Request,
    profile_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    profile = await _get_profile_or_404(profile_id, current_user, db)
    new_key = generate_profile_api_key()
    profile.api_key = new_key
    await db.commit()

    logger.info(f"API key regenerated: profile_id={profile_id}, user={current_user.username}")

    return templates.TemplateResponse("profile_key_once.html", {
        "request": request,
        "profile": profile,
        "api_key": new_key,
    })


@router.post("/profiles/{profile_id}/delete")
async def delete_profile(
    profile_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    profile = await _get_profile_or_404(profile_id, current_user, db)

    # Таймкоды удаляются каскадно (ondelete="CASCADE")
    await db.delete(profile)
    await db.commit()

    logger.info(f"Profile deleted: profile_id={profile_id}, user={current_user.username}")
    return RedirectResponse(url="/profiles", status_code=302)


@router.post("/profiles/{profile_id}/clear-timecodes")
async def clear_profile_timecodes(
    profile_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    await _get_profile_or_404(profile_id, current_user, db)

    await db.execute(delete(Timecode).where(Timecode.profile_id == profile_id))
    await db.commit()

    logger.info(f"Timecodes cleared: profile_id={profile_id}, user={current_user.username}")
    return RedirectResponse(url="/profiles", status_code=302)


# ---------------------------------------------------------------------------
# Device Activation Flow (для Lampa на ТВ/устройстве без удобного ввода)
# ---------------------------------------------------------------------------

@router.post("/device/code")
async def create_device_code(db: AsyncSession = Depends(get_db)):
    """
    Lampa запрашивает код активации.
    Возвращает одноразовый код (ABC-123) и время жизни.
    Lampa показывает этот код пользователю и начинает polling /device/status.
    """
    # Генерируем уникальный код (retry при коллизии)
    for _ in range(5):
        code = generate_device_code()
        existing = await db.execute(select(DeviceCode).where(DeviceCode.code == code))
        if not existing.scalar_one_or_none():
            break
    else:
        raise HTTPException(status_code=503, detail="Не удалось сгенерировать код")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=DEVICE_CODE_TTL_MINUTES)
    device_code = DeviceCode(code=code, expires_at=expires_at)
    db.add(device_code)
    await db.commit()

    return {
        "code": code,
        "expires_in": DEVICE_CODE_TTL_MINUTES * 60,
        "poll_interval": 3,
    }


class _LinkDeviceBody(BaseModel):
    code: str
    profile_id: int


@router.post("/device/link")
async def link_device(
    body: _LinkDeviceBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Пользователь на веб-странице вводит код из Lampa и выбирает профиль.
    Принимает JSON: {"code": "ABC-123", "profile_id": 1}.
    """
    if not current_user:
        raise HTTPException(status_code=401)

    code = body.code.strip().upper()
    profile_id = body.profile_id
    now = datetime.now(timezone.utc)

    result = await db.execute(select(DeviceCode).where(DeviceCode.code == code))
    device_code = result.scalar_one_or_none()

    if not device_code:
        raise HTTPException(status_code=404, detail="Код не найден")
    if device_code.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=410, detail="Код истёк")
    if device_code.profile_id is not None:
        raise HTTPException(status_code=409, detail="Код уже использован")

    # Проверяем что профиль принадлежит пользователю
    await _get_profile_or_404(profile_id, current_user, db)

    device_code.profile_id = profile_id
    device_code.user_id = current_user.id
    await db.commit()

    return {"success": True, "message": "Устройство привязано"}


@router.get("/device/status")
async def device_status(code: str, db: AsyncSession = Depends(get_db)):
    """
    Lampa polling этого эндпоинта каждые 3 секунды.
    Когда linked=true — возвращает api_key профиля, Lampa сохраняет и прекращает polling.
    """
    code = code.strip().upper()
    now = datetime.now(timezone.utc)

    result = await db.execute(select(DeviceCode).where(DeviceCode.code == code))
    device_code = result.scalar_one_or_none()

    if not device_code:
        raise HTTPException(status_code=404, detail="Код не найден")
    if device_code.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=410, detail="Код истёк")

    if device_code.profile_id is None:
        return {"linked": False}

    # Возвращаем api_key и удаляем использованный код
    result = await db.execute(select(Profile).where(Profile.id == device_code.profile_id))
    profile = result.scalar_one_or_none()

    await db.delete(device_code)
    await db.commit()

    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    return {
        "linked": True,
        "api_key": profile.api_key,
        "profile_name": profile.name,
    }
