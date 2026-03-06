import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func

from app.db.database import get_db
from app.db.models import Device, DeviceCode, Timecode, User, DEVICE_LIMITS
from app.utils import generate_profile_api_key, generate_device_code, validate_name
from app.api.dependencies import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

DEVICE_CODE_TTL_MINUTES = 10


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

async def _devices_with_stats(user_id: int, db: AsyncSession) -> list[dict]:
    result = await db.execute(select(Device).where(Device.user_id == user_id))
    devices = result.scalars().all()
    out = []
    for d in devices:
        cnt_result = await db.execute(
            select(func.count()).select_from(Timecode).where(Timecode.device_id == d.id)
        )
        out.append({
            "id": d.id, "name": d.name, "token": d.token,
            "created_at": d.created_at,
            "timecodes_count": cnt_result.scalar() or 0,
        })
    return out


async def _get_device_or_404(device_id: int, user: User, db: AsyncSession) -> Device:
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    return device


async def _check_device_limit(user: User, db: AsyncSession) -> None:
    limit = DEVICE_LIMITS.get(user.role, 3)
    if limit is None:
        return  # super — без ограничений
    cnt_result = await db.execute(
        select(func.count()).select_from(Device).where(Device.user_id == user.id)
    )
    count = cnt_result.scalar() or 0
    if count >= limit:
        role_names = {"simple": "базового", "premium": "премиум"}
        role_label = role_names.get(user.role, user.role)
        raise HTTPException(
            status_code=403,
            detail=f"Достигнут лимит устройств для {role_label} аккаунта ({limit} шт.)"
        )


# ---------------------------------------------------------------------------
# Веб-страница управления устройствами
# ---------------------------------------------------------------------------

@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    devices = await _devices_with_stats(current_user.id, db)
    limit = DEVICE_LIMITS.get(current_user.role, 3)
    return templates.TemplateResponse("profiles.html", {
        "request": request,
        "user": current_user,
        "profiles": devices,
        "device_limit": limit,
    })


# ---------------------------------------------------------------------------
# CRUD устройств
# ---------------------------------------------------------------------------

@router.post("/profiles/create")
async def create_device(
    request: Request,
    name: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    async def _err(msg):
        devices = await _devices_with_stats(current_user.id, db)
        limit = DEVICE_LIMITS.get(current_user.role, 3)
        return templates.TemplateResponse("profiles.html", {
            "request": request, "user": current_user, "profiles": devices,
            "device_limit": limit, "error": msg,
        }, status_code=400)

    name = name.strip()[:100]
    if not name:
        return await _err("Имя устройства не может быть пустым")
    is_valid, error_msg = validate_name(name)
    if not is_valid:
        return await _err(error_msg)

    try:
        await _check_device_limit(current_user, db)
    except HTTPException as e:
        return await _err(e.detail)

    token = generate_profile_api_key()
    device = Device(user_id=current_user.id, name=name, token=token)
    db.add(device)
    await db.commit()
    await db.refresh(device)

    logger.info(f"Device created: user={current_user.username}, name={name}, id={device.id}")

    return templates.TemplateResponse("profile_key_once.html", {
        "request": request,
        "profile": device,
        "api_key": token,
    })


@router.post("/profiles/{device_id}/rename")
async def rename_device(
    request: Request,
    device_id: int,
    name: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    device = await _get_device_or_404(device_id, current_user, db)
    new_name = name.strip()[:100]
    is_valid, error_msg = validate_name(new_name)
    if not is_valid:
        devices = await _devices_with_stats(current_user.id, db)
        limit = DEVICE_LIMITS.get(current_user.role, 3)
        return templates.TemplateResponse("profiles.html", {
            "request": request, "user": current_user, "profiles": devices,
            "device_limit": limit, "error": error_msg,
        }, status_code=400)
    device.name = new_name
    await db.commit()
    return RedirectResponse(url="/profiles", status_code=302)


@router.post("/profiles/{device_id}/regenerate")
async def regenerate_device_token(
    request: Request,
    device_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    device = await _get_device_or_404(device_id, current_user, db)
    new_token = generate_profile_api_key()
    device.token = new_token
    await db.commit()

    logger.info(f"Token regenerated: device_id={device_id}, user={current_user.username}")

    return templates.TemplateResponse("profile_key_once.html", {
        "request": request,
        "profile": device,
        "api_key": new_token,
    })


@router.post("/profiles/{device_id}/delete")
async def delete_device(
    device_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    device = await _get_device_or_404(device_id, current_user, db)
    await db.delete(device)
    await db.commit()

    logger.info(f"Device deleted: device_id={device_id}, user={current_user.username}")
    return RedirectResponse(url="/profiles", status_code=302)


@router.post("/profiles/{device_id}/clear-timecodes")
async def clear_device_timecodes(
    device_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    await _get_device_or_404(device_id, current_user, db)
    await db.execute(delete(Timecode).where(Timecode.device_id == device_id))
    await db.commit()

    logger.info(f"Timecodes cleared: device_id={device_id}, user={current_user.username}")
    return RedirectResponse(url="/profiles", status_code=302)


# ---------------------------------------------------------------------------
# Device Activation Flow (для Lampa на ТВ без удобного ввода)
# ---------------------------------------------------------------------------

@router.post("/device/code")
async def create_device_code(db: AsyncSession = Depends(get_db)):
    """
    Lampa запрашивает код активации.
    Возвращает одноразовый код (ABC-123) и время жизни.
    Lampa показывает этот код пользователю и начинает polling /device/status.
    """
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
    device_id: int | None = None
    device_name: str | None = None  # создать новое устройство с этим именем


@router.post("/device/link")
async def link_device(
    body: _LinkDeviceBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Пользователь вводит код из Lampa и выбирает (или создаёт) устройство.
    JSON: {"code": "483921", "device_id": 1}
       или {"code": "483921", "device_name": "Гостиная ТВ"}
    """
    if not current_user:
        raise HTTPException(status_code=401)

    if body.device_id is None and not body.device_name:
        raise HTTPException(status_code=400, detail="Укажите device_id или device_name")

    code = body.code.strip()
    now = datetime.now(timezone.utc)

    result = await db.execute(select(DeviceCode).where(DeviceCode.code == code))
    device_code = result.scalar_one_or_none()

    if not device_code:
        raise HTTPException(status_code=404, detail="Код не найден")
    if device_code.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=410, detail="Код истёк")
    if device_code.device_id is not None:
        raise HTTPException(status_code=409, detail="Код уже использован")

    if body.device_id is not None:
        # Привязка к существующему устройству
        device = await _get_device_or_404(body.device_id, current_user, db)
    else:
        # Создание нового устройства
        await _check_device_limit(current_user, db)
        name = (body.device_name or "Новое устройство").strip()[:100]
        token = generate_profile_api_key()
        device = Device(user_id=current_user.id, name=name, token=token)
        db.add(device)
        await db.flush()  # получаем device.id до commit
        logger.info(f"Device created via activation: user={current_user.username}, name={name}")

    device_code.device_id = device.id
    device_code.user_id = current_user.id
    await db.commit()

    return {"success": True, "message": "Устройство привязано", "device_name": device.name}


@router.get("/device/status")
async def device_status(code: str, db: AsyncSession = Depends(get_db)):
    """
    Lampa polling этого эндпоинта каждые 3 секунды.
    Когда linked=true — возвращает token устройства, Lampa сохраняет и прекращает polling.
    """
    code = code.strip().upper()
    now = datetime.now(timezone.utc)

    result = await db.execute(select(DeviceCode).where(DeviceCode.code == code))
    device_code = result.scalar_one_or_none()

    if not device_code:
        raise HTTPException(status_code=404, detail="Код не найден")
    if device_code.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=410, detail="Код истёк")

    if device_code.device_id is None:
        return {"linked": False}

    result = await db.execute(select(Device).where(Device.id == device_code.device_id))
    device = result.scalar_one_or_none()

    await db.delete(device_code)
    await db.commit()

    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    return {
        "linked": True,
        "token": device.token,
        "device_name": device.name,
    }
