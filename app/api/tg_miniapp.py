"""
Telegram Mini App — административная панель NUMParser.

GET  /tg-app               — HTML страница Mini App
POST /tg-app/api/auth      — валидация initData Telegram WebApp
GET  /tg-app/api/stats     — сводная статистика (только admins)
GET  /tg-app/api/users     — список пользователей (только admins)
POST /tg-app/api/users/{id}/role   — изменить роль пользователя
GET  /tg-app/api/messages          — сообщения поддержки
POST /tg-app/api/messages/{id}/reply — ответить на сообщение
POST /tg-app/api/messages/{id}/read  — пометить прочитанным
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.templates import get_templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.database import get_db
from app.db.models import (
    Device,
    LampaProfile,
    Session,
    SupportMessage,
    Timecode,
    TelegramUser,
    User,
    USER_ROLES,
)
from app import settings_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tg-app")
templates = get_templates()


# ─── Валидация initData ───────────────────────────────────────────────────────

def _validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет подпись Telegram WebApp initData.
    Возвращает распарсенные данные или None если подпись невалидна."""
    vals = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_val = vals.pop("hash", None)
    if not hash_val:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, hash_val):
        return None

    if "user" in vals:
        try:
            vals["user"] = json.loads(unquote(vals["user"]))
        except Exception:
            return None

    return vals


def _parse_init_data(x_telegram_init_data: str = Header(default="")) -> dict:
    """Зависимость: проверяет подпись initData, возвращает данные пользователя."""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")
    data = _validate_init_data(x_telegram_init_data, settings.TELEGRAM_BOT_TOKEN)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid Telegram initData")
    return data.get("user", {})


async def _require_admin(tg_user: dict = Depends(_parse_init_data)) -> dict:
    """Зависимость: проверяет права администратора."""
    tg_id = tg_user.get("id")
    if tg_id not in get_settings().telegram_admin_id_list:
        raise HTTPException(status_code=403, detail="Admin access required")
    return tg_user


async def _require_linked_user(
    tg_user: dict = Depends(_parse_init_data),
    db: AsyncSession = Depends(get_db),
) -> tuple[dict, User]:
    """Зависимость: проверяет, что Telegram привязан к аккаунту, возвращает (tg_user, User)."""
    tg_id = tg_user.get("id")
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == tg_id)
    )
    tg_link = result.scalar_one_or_none()
    if not tg_link:
        raise HTTPException(status_code=403, detail="Telegram not linked to any account")
    user_result = await db.execute(select(User).where(User.id == tg_link.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")
    return tg_user, user


# ─── Страница Mini App ────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def miniapp_page(request: Request):
    return templates.TemplateResponse("tg_miniapp.html", {"request": request})


# ─── Auth endpoint ────────────────────────────────────────────────────────────

@router.post("/api/auth")
async def miniapp_auth(request: Request, db: AsyncSession = Depends(get_db)):
    """Валидирует initData, возвращает is_admin и статус привязки аккаунта."""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Bot not configured")

    body = await request.json()
    init_data = body.get("initData", "")
    data = _validate_init_data(init_data, settings.TELEGRAM_BOT_TOKEN)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid initData")

    user_info = data.get("user", {})
    tg_id = user_info.get("id")
    is_admin = tg_id in settings.telegram_admin_id_list

    # Проверяем привязку к аккаунту
    tg_result = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == tg_id)
    )
    tg_link = tg_result.scalar_one_or_none()
    is_linked = tg_link is not None

    return {
        "ok": True,
        "is_admin": is_admin,
        "is_linked": is_linked,
        "user": {
            "id": tg_id,
            "first_name": user_info.get("first_name", ""),
            "username": user_info.get("username", ""),
        },
    }


# ─── User: my account ────────────────────────────────────────────────────────

@router.get("/api/me")
async def miniapp_me(
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Информация об аккаунте + список устройств текущего пользователя."""
    _tg_user, user = linked
    result = await db.execute(select(Device).where(Device.user_id == user.id))
    devices = result.scalars().all()

    devices_data = []
    for d in devices:
        tc_count = await db.scalar(
            select(func.count()).select_from(Timecode).where(Timecode.device_id == d.id)
        )
        devices_data.append({
            "id": d.id,
            "name": d.name,
            "timecodes_count": tc_count or 0,
            "created_at": d.created_at.strftime("%d.%m.%Y") if d.created_at else None,
        })

    limit = settings_cache.get_role_limit(user.role, "device_limit")
    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}

    return {
        "username": user.username,
        "role": user.role,
        "role_label": role_labels.get(user.role, user.role),
        "device_count": len(devices_data),
        "device_limit": limit,
        "devices": devices_data,
    }


class RenameDevice(BaseModel):
    name: str


class ReplyBody(BaseModel):
    text: str


@router.post("/api/me/devices/{device_id}/rename")
async def miniapp_rename_device(
    device_id: int,
    body: RenameDevice,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    name = body.name.strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    device.name = name
    await db.commit()
    return {"ok": True, "name": name}


@router.post("/api/me/devices/{device_id}/delete")
async def miniapp_delete_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    await db.delete(device)
    await db.commit()
    return {"ok": True}


@router.post("/api/me/devices/{device_id}/regenerate")
async def miniapp_regenerate_token(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Пересоздать токен устройства."""
    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.utils import generate_profile_api_key
    device.token = generate_profile_api_key()
    await db.commit()
    return {"ok": True, "token": device.token}


@router.post("/api/me/devices/{device_id}/clear-timecodes")
async def miniapp_clear_timecodes(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Удалить всю историю просмотров устройства."""
    from sqlalchemy import delete as sa_delete
    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Device not found")

    await db.execute(sa_delete(Timecode).where(Timecode.device_id == device_id))
    await db.commit()
    return {"ok": True}


class ActivateBody(BaseModel):
    code: str


@router.post("/api/me/devices/{device_id}/activate")
async def miniapp_activate_device(
    device_id: int,
    body: ActivateBody,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Привязать Lampa к устройству по коду активации (который Lampa показывает на экране)."""
    from app.db.models import DeviceCode
    from datetime import timezone

    _tg_user, user = linked
    device_result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    device = device_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    code = body.code.strip().upper()
    now = datetime.now(timezone.utc)

    code_result = await db.execute(select(DeviceCode).where(DeviceCode.code == code))
    device_code = code_result.scalar_one_or_none()

    if not device_code:
        raise HTTPException(status_code=404, detail="Код не найден")
    if device_code.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=410, detail="Код истёк")
    if device_code.device_id is not None:
        raise HTTPException(status_code=409, detail="Код уже использован")

    device_code.device_id = device.id
    device_code.user_id = user.id
    await db.commit()

    return {"ok": True, "device_name": device.name}


@router.get("/api/me/devices/{device_id}")
async def miniapp_device_details(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Токен устройства + список Lampa-профилей."""
    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    profiles_result = await db.execute(
        select(LampaProfile)
        .where(LampaProfile.device_id == device_id)
        .order_by(LampaProfile.id)
    )
    profiles = profiles_result.scalars().all()

    profile_limit = settings_cache.get_role_limit(user.role, "profile_limit")

    return {
        "id": device.id,
        "name": device.name,
        "token": device.token,
        "profile_limit": profile_limit,
        "profiles": [
            {"profile_id": p.lampa_profile_id, "name": p.name}
            for p in profiles
        ],
    }


class CreateProfile(BaseModel):
    name: str
    profile_id: str | None = None


@router.post("/api/me/devices/{device_id}/profiles/create")
async def miniapp_create_profile(
    device_id: int,
    body: CreateProfile,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Device not found")

    name = body.name.strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail="Название профиля не может быть пустым")

    # Проверка лимита профилей
    limit = settings_cache.get_role_limit(user.role, "profile_limit")
    if limit is not None:
        count = await db.scalar(
            select(func.count()).select_from(LampaProfile)
            .where(LampaProfile.device_id == device_id)
        )
        if (count or 0) >= limit:
            raise HTTPException(
                status_code=403,
                detail=f"Достигнут лимит профилей ({limit}) для вашего тарифа",
            )

    import secrets as _secrets
    profile_id = (body.profile_id or "").strip().strip("_")[:100] or _secrets.token_hex(4)

    existing = await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device_id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Профиль с таким ID уже существует")

    lp = LampaProfile(device_id=device_id, lampa_profile_id=profile_id, name=name)
    db.add(lp)
    await db.commit()
    return {"ok": True, "profile_id": profile_id, "name": name}


@router.post("/api/me/devices/{device_id}/profiles/{profile_id}/delete")
async def miniapp_delete_profile(
    device_id: int,
    profile_id: str,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    from sqlalchemy import delete as sa_delete
    from app.db.models import Timecode as _Timecode

    _tg_user, user = linked
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Device not found")

    lp_result = await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device_id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )
    lp = lp_result.scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    await db.execute(
        sa_delete(_Timecode).where(
            _Timecode.device_id == device_id,
            _Timecode.lampa_profile_id == profile_id,
        )
    )
    await db.delete(lp)
    await db.commit()
    return {"ok": True}


class CreateDevice(BaseModel):
    name: str


@router.post("/api/me/devices/create")
async def miniapp_create_device(
    body: CreateDevice,
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Создать новое устройство. Возвращает токен (показывается один раз)."""
    _tg_user, user = linked

    name = body.name.strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail="Имя устройства не может быть пустым")

    # Проверка лимита
    limit = settings_cache.get_role_limit(user.role, "device_limit")
    if limit is not None:
        count = await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == user.id)
        )
        if (count or 0) >= limit:
            raise HTTPException(
                status_code=403,
                detail=f"Достигнут лимит устройств ({limit}) для вашего тарифа",
            )

    from app.utils import generate_profile_api_key
    token = generate_profile_api_key()
    device = Device(user_id=user.id, name=name, token=token)
    db.add(device)
    await db.commit()
    await db.refresh(device)

    logger.info(f"Mini App: device created user={user.username} name={name}")
    return {"ok": True, "id": device.id, "name": device.name, "token": token}


# ─── Unlink Telegram ──────────────────────────────────────────────────────────

@router.post("/api/me/unlink")
async def miniapp_unlink(
    db: AsyncSession = Depends(get_db),
    linked: tuple = Depends(_require_linked_user),
):
    """Отвязать Telegram от аккаунта."""
    _tg_user, user = linked
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.user_id == user.id)
    )
    tg_link = result.scalar_one_or_none()
    if tg_link:
        await db.delete(tg_link)
        await db.commit()
    logger.info(f"Mini App: Telegram unlinked for user={user.username}")
    return {"ok": True}


# ─── Stats ────────────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def miniapp_stats(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(_require_admin),
):
    from datetime import date, timedelta
    from app.db.models import Timecode, MediaCard

    total_users   = await db.scalar(select(func.count()).select_from(User))
    total_devices = await db.scalar(select(func.count()).select_from(Device))
    total_tcs     = await db.scalar(select(func.count()).select_from(Timecode))

    role_counts = {}
    for role in USER_ROLES:
        cnt = await db.scalar(
            select(func.count()).select_from(User).where(User.role == role)
        )
        role_counts[role] = cnt or 0

    tg_linked = await db.scalar(select(func.count()).select_from(TelegramUser))

    # Пользователи с premium_until (активные подписки)
    from sqlalchemy import and_
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    active_premium = await db.scalar(
        select(func.count()).select_from(User).where(
            and_(User.role == "premium", User.premium_until.isnot(None), User.premium_until > now)
        )
    )

    # Новые пользователи за сегодня
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = await db.scalar(
        select(func.count()).select_from(User).where(User.created_at >= today_start)
    )

    # Новые пользователи за 7 дней
    week_start = today_start - timedelta(days=7)
    new_users_week = await db.scalar(
        select(func.count()).select_from(User).where(User.created_at >= week_start)
    )

    # Таймкоды за сегодня
    tcs_today = await db.scalar(
        select(func.count()).select_from(Timecode).where(Timecode.updated_at >= today_start)
    )

    # Размер TMDB-кеша
    media_cards = await db.scalar(select(func.count()).select_from(MediaCard))

    unread_support = await db.scalar(
        select(func.count())
        .select_from(SupportMessage)
        .where(SupportMessage.direction == "in", SupportMessage.is_read == False)  # noqa: E712
    )

    return {
        "total_users":     total_users or 0,
        "total_devices":   total_devices or 0,
        "total_timecodes": total_tcs or 0,
        "role_counts":     role_counts,
        "active_premium":  active_premium or 0,
        "tg_linked":       tg_linked or 0,
        "new_users_today": new_users_today or 0,
        "new_users_week":  new_users_week or 0,
        "tcs_today":       tcs_today or 0,
        "media_cards":     media_cards or 0,
        "unread_support":  unread_support or 0,
    }


# ─── Users ────────────────────────────────────────────────────────────────────

@router.get("/api/users")
async def miniapp_users(
    q: str = "",
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(_require_admin),
):
    query = select(User).order_by(User.id)
    result = await db.execute(query)
    users = result.scalars().all()

    # Фильтр по поиску
    if q:
        q_lower = q.lower()
        users = [u for u in users if q_lower in u.username.lower()]

    users_data = []
    for u in users:
        device_count = await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == u.id)
        )
        tg_result = await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == u.id)
        )
        tg = tg_result.scalar_one_or_none()
        limit = settings_cache.get_role_limit(u.role, "device_limit")

        users_data.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "device_count": device_count or 0,
            "device_limit": limit,
            "tg_username": tg.username if tg else None,
            "tg_id": tg.telegram_id if tg else None,
            "created_at": u.created_at.strftime("%d.%m.%Y") if u.created_at else None,
            "blocked_at": u.blocked_at.strftime("%d.%m.%Y") if u.blocked_at else None,
            "block_reason": u.block_reason,
        })

    return {"users": users_data}


class RoleChange(BaseModel):
    role: str


@router.post("/api/users/{user_id}/role")
async def miniapp_set_role(
    user_id: int,
    body: RoleChange,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    if body.role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role: {body.role}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = user.role
    user.role = body.role
    await db.commit()

    logger.info(
        f"Mini App admin {admin.get('username', admin.get('id'))}: "
        f"user {user.username} role {old_role} → {body.role}"
    )
    return {"ok": True, "username": user.username, "role": body.role}


class BlockBody(BaseModel):
    reason: str = ""


@router.post("/api/users/{user_id}/block")
async def miniapp_block_user(
    user_id: int,
    body: BlockBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from sqlalchemy import delete as sa_delete
    user.blocked_at = datetime.now(timezone.utc)
    user.block_reason = body.reason.strip() or None
    await db.execute(sa_delete(Session).where(Session.user_id == user_id))
    await db.commit()

    logger.info(
        f"Mini App admin {admin.get('username', admin.get('id'))}: "
        f"user {user.username} blocked, reason={body.reason!r}"
    )
    return {"ok": True, "username": user.username}


@router.post("/api/users/{user_id}/unblock")
async def miniapp_unblock_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.blocked_at = None
    user.block_reason = None
    await db.commit()

    logger.info(
        f"Mini App admin {admin.get('username', admin.get('id'))}: "
        f"user {user.username} unblocked"
    )
    return {"ok": True, "username": user.username}


# ─── Support messages (admin) ─────────────────────────────────────────────────

@router.get("/api/messages")
async def miniapp_messages(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(_require_admin),
):
    result = await db.execute(
        select(SupportMessage).order_by(SupportMessage.created_at.desc()).limit(200)
    )
    messages = result.scalars().all()

    conversations: dict[int, dict] = {}
    for m in reversed(messages):
        uid = m.user_telegram_id
        if uid not in conversations:
            conversations[uid] = {
                "user_telegram_id": uid,
                "user_username": m.user_username,
                "messages": [],
                "has_unread": False,
            }
        conversations[uid]["messages"].append({
            "id": m.id,
            "direction": m.direction,
            "text": m.text,
            "is_read": m.is_read,
            "created_at": m.created_at.strftime("%d.%m.%Y %H:%M") if m.created_at else None,
        })
        if m.direction == "in" and not m.is_read:
            conversations[uid]["has_unread"] = True

    conv_list = sorted(
        conversations.values(),
        key=lambda c: (not c["has_unread"], -(c["messages"][-1]["id"] if c["messages"] else 0)),
    )
    return {"conversations": conv_list}


@router.post("/api/messages/{user_telegram_id}/reply")
async def miniapp_reply(
    user_telegram_id: int,
    body: ReplyBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(_require_admin),
):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    from app.bot import send_message
    ok = await send_message(user_telegram_id, f"💬 <b>Ответ от поддержки:</b>\n\n{body.text}")
    if not ok:
        raise HTTPException(status_code=502, detail="Не удалось отправить сообщение")

    db.add(SupportMessage(
        user_telegram_id=user_telegram_id,
        direction="out",
        text=body.text,
        admin_telegram_id=admin.get("id"),
        is_read=True,
    ))
    await db.commit()
    return {"ok": True}


@router.post("/api/messages/{user_telegram_id}/read")
async def miniapp_mark_read(
    user_telegram_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(_require_admin),
):
    from sqlalchemy import update
    await db.execute(
        update(SupportMessage)
        .where(
            SupportMessage.user_telegram_id == user_telegram_id,
            SupportMessage.direction == "in",
            SupportMessage.is_read == False,  # noqa: E712
        )
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


# ─── Настройки приложения ──────────────────────────────────────────────────────

@router.get("/api/settings")
async def tg_get_settings(
    _admin: dict = Depends(_require_admin),
):
    """Вернуть все настройки приложения с метаданными для UI."""
    current = settings_cache.all_settings()
    groups_out = []
    for group_name, keys in settings_cache.GROUPS:
        items = [
            {
                "key": k,
                "label": settings_cache.LABELS.get(k, k),
                "value": current.get(k, settings_cache.DEFAULTS.get(k, "")),
                "default": settings_cache.DEFAULTS.get(k, ""),
            }
            for k in keys
        ]
        groups_out.append({"name": group_name, "items": items})
    return {"groups": groups_out}


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.post("/api/settings")
async def tg_update_setting(
    body: SettingUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(_require_admin),
):
    """Обновить одну настройку приложения."""
    if body.key not in settings_cache.DEFAULTS:
        raise HTTPException(status_code=400, detail=f"Неизвестный ключ: {body.key}")
    await settings_cache.set_setting(body.key, body.value.strip(), db)
    return {"ok": True, "key": body.key, "value": body.value.strip()}
