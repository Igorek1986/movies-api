import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, distinct
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.database import get_db
from app.db.models import Device, DeviceCode, Timecode, MediaCard, LampaProfile, User, DEVICE_LIMITS, TelegramUser

LAMPA_PROFILE_LIMITS: dict[str, int | None] = {"simple": 3, "premium": 8, "super": None}

_CARD_ID_RE = re.compile(r"^(\d+)_(movie|tv)$")
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
    tg_result = await db.execute(
        select(TelegramUser).where(TelegramUser.user_id == current_user.id)
    )
    tg = tg_result.scalar_one_or_none()
    return templates.TemplateResponse("profiles.html", {
        "request": request,
        "user": current_user,
        "profiles": devices,
        "device_limit": limit,
        "tg_linked": tg is not None,
        "tg_username": tg.username if (tg and tg.username) else None,
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


# ---------------------------------------------------------------------------
# API: история просмотров (веб-авторизация)
# ---------------------------------------------------------------------------

@router.get("/api/history")
async def api_history(
    device_id: int = Query(...),
    profile_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    q = select(Timecode).where(Timecode.device_id == device_id)
    if profile_id is not None:
        q = q.where(Timecode.lampa_profile_id == profile_id)
    result = await db.execute(q.order_by(Timecode.updated_at.desc()))
    timecodes = result.scalars().all()

    card_agg: dict[str, dict] = {}
    for tc in timecodes:
        if not _CARD_ID_RE.match(tc.card_id):
            continue
        try:
            pct = json.loads(tc.data).get("percent", 0)
        except Exception:
            pct = 0
        if tc.card_id not in card_agg:
            card_agg[tc.card_id] = {"last_watched": tc.updated_at, "max_percent": pct}
        else:
            if pct > card_agg[tc.card_id]["max_percent"]:
                card_agg[tc.card_id]["max_percent"] = pct

    if not card_agg:
        return []

    mc_result = await db.execute(
        select(MediaCard).where(MediaCard.card_id.in_(list(card_agg.keys())))
    )
    media_cards = {mc.card_id: mc for mc in mc_result.scalars().all()}

    history = []
    for card_id, agg in card_agg.items():
        mc = media_cards.get(card_id)
        m = _CARD_ID_RE.match(card_id)
        history.append({
            "card_id": card_id,
            "media_type": mc.media_type if mc else (m.group(2) if m else None),
            "title": mc.title if mc else None,
            "poster_path": mc.poster_path if mc else None,
            "year": mc.year if mc else None,
            "release_date": mc.release_date if mc else None,
            "last_watched": agg["last_watched"].isoformat() if agg["last_watched"] else None,
            "max_percent": agg["max_percent"],
        })

    history.sort(key=lambda x: x["last_watched"] or "", reverse=True)
    return history


@router.get("/api/profile-ids")
async def api_profile_ids(
    device_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает список уникальных lampa_profile_id с именами для устройства."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    # Профили из LampaProfile (включая созданные вручную без таймкодов)
    lp_result = await db.execute(
        select(LampaProfile).where(LampaProfile.device_id == device_id)
    )
    lp_map = {lp.lampa_profile_id: lp.name for lp in lp_result.scalars().all()}

    # Профили из таймкодов (могут быть не в LampaProfile)
    tc_result = await db.execute(
        select(distinct(Timecode.lampa_profile_id))
        .where(Timecode.device_id == device_id)
    )
    tc_ids = {r[0] for r in tc_result.all()}

    all_ids = sorted((lp_map.keys() | tc_ids) - {""})

    # "Основной" (пустой profile_id) доступен если у него есть таймкоды
    # ИЛИ если лимит профилей не исчерпан
    lp_count = len(lp_map)
    limit = LAMPA_PROFILE_LIMITS.get(current_user.role, 3)
    основной_has_tc = "" in tc_ids
    основной_available = основной_has_tc or (limit is None or lp_count < limit)

    return {
        "profiles": [
            {"profile_id": pid, "name": lp_map.get(pid, "")}
            for pid in all_ids
        ],
        "основной_available": основной_available,
    }


class _ProfileNameBody(BaseModel):
    device_id: int
    profile_id: str
    name: str


@router.post("/api/profile-name")
async def api_set_profile_name(
    body: _ProfileNameBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сохраняет/обновляет человеческое название для lampa_profile_id."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(body.device_id, current_user, db)

    name = body.name.strip()[:100]

    # Если запись уже есть — просто обновляем имя; если нет — проверяем лимит
    existing = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == body.device_id,
            LampaProfile.lampa_profile_id == body.profile_id,
        )
    )).scalar_one_or_none()

    if not existing:
        count = (await db.execute(
            select(func.count()).select_from(LampaProfile)
            .where(LampaProfile.device_id == body.device_id)
        )).scalar() or 0
        limit = LAMPA_PROFILE_LIMITS.get(current_user.role, 3)
        if limit is not None and count >= limit:
            raise HTTPException(status_code=403, detail="Достигнут лимит профилей")

    stmt = pg_insert(LampaProfile).values(
        device_id=body.device_id,
        lampa_profile_id=body.profile_id,
        name=name,
    ).on_conflict_do_update(
        constraint="uq_lampa_profile",
        set_={"name": name},
    )
    await db.execute(stmt)
    await db.commit()
    return {"ok": True}


class _ProfileCreateBody(BaseModel):
    device_id: int
    name: str
    profile_id: str | None = None  # если не указан — генерируем


@router.post("/api/lampa-profile/create")
async def api_create_lampa_profile(
    body: _ProfileCreateBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создаёт LampaProfile запись. Проверяет лимит по роли пользователя."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(body.device_id, current_user, db)

    # Считаем существующие профили устройства
    count_result = await db.execute(
        select(func.count()).select_from(LampaProfile)
        .where(LampaProfile.device_id == body.device_id)
    )
    count = count_result.scalar() or 0

    limit = LAMPA_PROFILE_LIMITS.get(current_user.role, 3)
    if limit is not None and count >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Достигнут лимит профилей ({limit}) для вашего тарифа",
        )

    name = body.name.strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail="Название профиля не может быть пустым")

    profile_id = (body.profile_id or "").strip().lstrip("_")[:100] or secrets.token_hex(4)

    # Проверяем уникальность profile_id для устройства
    existing = await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == body.device_id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Профиль с таким ID уже существует")

    lp = LampaProfile(device_id=body.device_id, lampa_profile_id=profile_id, name=name)
    db.add(lp)
    await db.commit()
    await db.refresh(lp)

    return {"ok": True, "profile_id": profile_id, "name": name}


@router.delete("/api/lampa-profile")
async def api_delete_lampa_profile(
    device_id: int = Query(...),
    profile_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет LampaProfile запись и все таймкоды профиля."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    result = await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == device_id,
            LampaProfile.lampa_profile_id == profile_id,
        )
    )
    lp = result.scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    await db.execute(delete(Timecode).where(
        Timecode.device_id == device_id,
        Timecode.lampa_profile_id == profile_id,
    ))
    await db.delete(lp)
    await db.commit()
    return {"ok": True}


@router.get("/api/lampa-profile/quota")
async def api_lampa_profile_quota(
    device_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    count_result = await db.execute(
        select(func.count()).select_from(LampaProfile)
        .where(LampaProfile.device_id == device_id)
    )
    count = count_result.scalar() or 0
    limit = LAMPA_PROFILE_LIMITS.get(current_user.role, 3)
    return {"count": count, "limit": limit}


# ---------------------------------------------------------------------------
# API: детали медиакарточки (TMDB)
# ---------------------------------------------------------------------------

def _mc_to_dict(mc: MediaCard) -> dict:
    return {
        "card_id": mc.card_id,
        "tmdb_id": mc.tmdb_id,
        "media_type": mc.media_type,
        "title": mc.title,
        "original_title": mc.original_title,
        "poster_path": mc.poster_path,
        "backdrop_path": mc.backdrop_path,
        "overview": mc.overview,
        "vote_average": mc.vote_average,
        "year": mc.year,
        "release_date": mc.release_date,
        "last_air_date": mc.last_air_date,
        "number_of_seasons": mc.number_of_seasons,
    }


@router.get("/api/media-card/{card_id}")
async def api_media_card(
    card_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)

    m = _CARD_ID_RE.match(card_id)
    if not m:
        raise HTTPException(status_code=400, detail="Неверный card_id")

    tmdb_id, media_type = int(m.group(1)), m.group(2)

    result = await db.execute(select(MediaCard).where(MediaCard.card_id == card_id))
    mc = result.scalar_one_or_none()

    if mc and mc.overview:
        return _mc_to_dict(mc)

    # Запрашиваем свежие данные из TMDB
    settings = get_settings()
    if not settings.TMDB_TOKEN:
        if mc:
            return _mc_to_dict(mc)
        raise HTTPException(status_code=404, detail="TMDB недоступен")

    title_key = "name" if media_type == "tv" else "title"
    orig_key = "original_name" if media_type == "tv" else "original_title"
    date_key = "first_air_date" if media_type == "tv" else "release_date"
    headers = {"Authorization": settings.TMDB_TOKEN, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                headers=headers,
                params={"language": "ru-RU"},
            )
    except Exception as e:
        logger.warning(f"TMDB request failed for {card_id}: {e}")
        if mc:
            return _mc_to_dict(mc)
        raise HTTPException(status_code=502, detail="Ошибка TMDB")

    if resp.status_code != 200:
        if mc:
            return _mc_to_dict(mc)
        raise HTTPException(status_code=404, detail="Не найдено в TMDB")

    data = resp.json()
    date = data.get(date_key) or ""
    values: dict = {
        "card_id": card_id,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": data.get(title_key) or "",
        "original_title": data.get(orig_key) or "",
        "poster_path": data.get("poster_path") or "",
        "backdrop_path": data.get("backdrop_path") or "",
        "overview": data.get("overview") or "",
        "vote_average": data.get("vote_average"),
        "year": date[:4],
        "release_date": date,
    }
    if media_type == "tv":
        seasons = data.get("seasons")
        values["last_air_date"] = data.get("last_air_date") or ""
        values["number_of_seasons"] = data.get("number_of_seasons")
        values["seasons_json"] = json.dumps(seasons, ensure_ascii=False) if seasons else None
        last_ep = data.get("last_episode_to_air") or {}
        values["last_ep_season"] = last_ep.get("season_number")
        values["last_ep_number"] = last_ep.get("episode_number")

    stmt = pg_insert(MediaCard).values([values])
    stmt = stmt.on_conflict_do_update(
        index_elements=["card_id"],
        set_={k: stmt.excluded[k] for k in values if k != "card_id"},
    )
    await db.execute(stmt)
    await db.commit()

    return {
        "card_id": values["card_id"],
        "tmdb_id": values["tmdb_id"],
        "media_type": values["media_type"],
        "title": values.get("title"),
        "original_title": values.get("original_title"),
        "poster_path": values.get("poster_path"),
        "backdrop_path": values.get("backdrop_path"),
        "overview": values.get("overview"),
        "vote_average": values.get("vote_average"),
        "year": values.get("year"),
        "release_date": values.get("release_date"),
        "last_air_date": values.get("last_air_date"),
        "number_of_seasons": values.get("number_of_seasons"),
    }
