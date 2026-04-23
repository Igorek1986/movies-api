import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone, date as _date

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templates import get_templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, distinct, case
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.database import get_db
from app.db.models import Device, DeviceCode, Timecode, MediaCard, LampaProfile, User, TelegramUser, Episode
from app import rate_limit, settings_cache
from app.api.timecodes import _trim_to_limit, _update_card_views


def _import_ctx(user: User) -> dict:
    """Переменные для шаблона profiles.html: импорт, синхронизация, лимиты."""
    daily_limit = settings_cache.get_role_limit(user.role, "import_daily")
    if daily_limit is not None:
        allowed, wait_sec, remaining = rate_limit.can_import(user.id, daily_limit)
    else:
        allowed, wait_sec, remaining = True, 0, None

    myshows_limit = settings_cache.get_role_limit(user.role, "myshows_daily")
    if user.role == "simple":
        sync_allowed, sync_wait_sec = False, 0
    elif myshows_limit is None:
        sync_allowed, sync_wait_sec = True, 0
    else:
        sync_allowed, sync_wait_sec = rate_limit.peek_sync(user.id)

    return {
        "import_allowed": allowed,
        "import_wait_sec": wait_sec,
        "import_daily_limit": daily_limit,
        "import_remaining": remaining,
        "sync_allowed": sync_allowed,
        "sync_wait_sec": sync_wait_sec,
        "timecode_limit": settings_cache.get_role_limit(user.role, "timecode_limit"),
        "profile_limit": settings_cache.get_role_limit(user.role, "profile_limit"),
    }

_CARD_ID_RE = re.compile(r"^(\d+)_(movie|tv)$")
from app.utils import generate_profile_api_key, generate_device_code, validate_name, lampa_hash, build_episode_hash_string, backup_codes_count
from app.api.dependencies import get_current_user, get_device_by_token

logger = logging.getLogger(__name__)
router = APIRouter()
templates = get_templates()


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
    limit = settings_cache.get_role_limit(user.role, "device_limit")
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
    limit = settings_cache.get_role_limit(current_user.role, "device_limit")
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
        "totp_enabled": current_user.totp_enabled,
        "backup_codes_count": backup_codes_count(current_user.backup_codes),
        "notifications_enabled": current_user.notifications_enabled is not False,
        "notify_start": current_user.notify_start if current_user.notify_start is not None else 9,
        "notify_end":   current_user.notify_end   if current_user.notify_end   is not None else 22,
        "user_timezone": current_user.timezone or "",
        "success": request.query_params.get("success"),
        **_import_ctx(current_user),
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
        limit = settings_cache.get_role_limit(current_user.role, "device_limit")
        return templates.TemplateResponse("profiles.html", {
            "request": request, "user": current_user, "profiles": devices,
            "device_limit": limit, "error": msg, **_import_ctx(current_user),
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
        limit = settings_cache.get_role_limit(current_user.role, "device_limit")
        return templates.TemplateResponse("profiles.html", {
            "request": request, "user": current_user, "profiles": devices,
            "device_limit": limit, "error": error_msg, **_import_ctx(current_user),
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

    device = await _get_device_or_404(device_id, current_user, db)
    await db.execute(delete(Timecode).where(Timecode.device_id == device_id))
    await db.commit()

    logger.info(f"Timecodes cleared: device_id={device_id}, user={current_user.username}")
    from urllib.parse import quote
    return RedirectResponse(url=f"/profiles?success={quote(f'Таймкоды устройства «{device.name}» удалены')}", status_code=302)


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

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings_cache.get_int("device_code_ttl_minutes"))
    device_code = DeviceCode(code=code, expires_at=expires_at)
    db.add(device_code)
    await db.commit()

    return {
        "code": code,
        "expires_in": settings_cache.get_int("device_code_ttl_minutes") * 60,
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

    _WATCHED_PCT = 90

    card_agg: dict[str, dict] = {}
    for tc in timecodes:
        if not _CARD_ID_RE.match(tc.card_id):
            continue
        try:
            pct = json.loads(tc.data).get("percent", 0)
        except Exception:
            pct = 0
        if tc.card_id not in card_agg:
            card_agg[tc.card_id] = {"last_watched": tc.updated_at, "items": {}}
        agg = card_agg[tc.card_id]
        agg["items"][tc.item] = max(agg["items"].get(tc.item, 0), pct)

    if not card_agg:
        return []

    mc_result = await db.execute(
        select(MediaCard).where(MediaCard.card_id.in_(list(card_agg.keys())))
    )
    media_cards = {mc.card_id: mc for mc in mc_result.scalars().all()}

    # Батч-запрос неспешловых эпизодов из MyShows для TV-сериалов
    tv_tmdb_ids = [
        int(_CARD_ID_RE.match(cid).group(1))
        for cid in card_agg.keys()
        if cid.endswith("_tv") and _CARD_ID_RE.match(cid)
    ]
    episodes_by_show: dict[int, list[tuple[int, int]]] = {}
    if tv_tmdb_ids:
        ep_rows = await db.execute(
            select(Episode.tmdb_show_id, Episode.season, Episode.episode)
            .where(Episode.tmdb_show_id.in_(tv_tmdb_ids), Episode.is_special == False, Episode.season > 0,  # noqa: E712
                   (Episode.air_date == None) | (Episode.air_date <= _date.today()))  # noqa: E711
            .order_by(Episode.tmdb_show_id, Episode.season, Episode.episode)
        )
        for tid, s, e in ep_rows.all():
            episodes_by_show.setdefault(tid, []).append((s, e))

    today_str = _date.today().isoformat()
    history = []
    for card_id, agg in card_agg.items():
        mc = media_cards.get(card_id)
        if not mc:
            continue

        items = agg["items"]
        max_pct = max(items.values(), default=0)

        watched_episodes = total_episodes = None
        is_ongoing = False
        progress = max_pct

        if card_id.endswith("_tv"):
            try:
                last_ep_s = mc.last_ep_season or 0
                last_ep_e = mc.last_ep_number or 0

                if mc.next_ep_air_date is not None:
                    is_ongoing = bool(mc.next_ep_air_date) or bool(
                        mc.last_air_date and mc.last_air_date > today_str
                    )

                # Приоритет: таблица episodes (MyShows, без спешлов)
                show_eps = episodes_by_show.get(mc.tmdb_id)
                if show_eps:
                    # MyShows хранит только вышедшие серии — фильтр по TMDB last_ep не нужен
                    aired = show_eps

                    orig = mc.original_title or ""
                    valid_hashes = {
                        lampa_hash(build_episode_hash_string(s, e, orig))
                        for s, e in aired
                    }
                    total_aired = len(aired)
                    watched_episodes = sum(
                        1 for h, p in items.items() if h in valid_hashes and p >= _WATCHED_PCT
                    )
                    total_episodes = total_aired
                    if mc.next_ep_air_date is None and mc.seasons_json:
                        try:
                            seasons = json.loads(mc.seasons_json)
                            total_all = sum(
                                s.get("episode_count", 0) for s in seasons
                                if (s.get("season_number") or 0) > 0
                            )
                            is_ongoing = (total_all > total_aired) or bool(
                                mc.last_air_date and mc.last_air_date > today_str
                            )
                        except Exception:
                            pass

                elif mc.seasons_json:
                    # Fallback: TMDB seasons_json
                    seasons = json.loads(mc.seasons_json)
                    total_aired = 0
                    total_all = 0
                    for s in seasons:
                        snum = s.get("season_number") or 0
                        if snum == 0:
                            continue
                        ep_count = s.get("episode_count") or 0
                        total_all += ep_count
                        if last_ep_s > 0:
                            if snum < last_ep_s:
                                total_aired += ep_count
                            elif snum == last_ep_s:
                                total_aired += last_ep_e
                        else:
                            s_air = s.get("air_date") or ""
                            if s_air and s_air <= today_str:
                                total_aired += ep_count
                    watched_episodes = sum(1 for p in items.values() if p >= _WATCHED_PCT)
                    total_episodes = total_aired
                    if mc.next_ep_air_date is None:
                        is_ongoing = (total_all > total_aired) or bool(
                            mc.last_air_date and mc.last_air_date > today_str
                        )

                if total_episodes is not None and total_episodes > 0:
                    progress = min(round((watched_episodes or 0) / total_episodes * 100), 100)
            except Exception:
                pass

        is_complete = (
            (watched_episodes is not None and total_episodes is not None
             and watched_episodes >= total_episodes > 0)
            if card_id.endswith("_tv")
            else progress >= _WATCHED_PCT
        )

        history.append({
            "card_id": card_id,
            "media_type": mc.media_type,
            "title": mc.title,
            "poster_path": mc.poster_path,
            "year": mc.year,
            "release_date": mc.release_date,
            "last_watched": agg["last_watched"].isoformat() if agg["last_watched"] else None,
            "max_percent": max_pct,
            "progress": progress,
            "watched_episodes": watched_episodes,
            "total_episodes": total_episodes,
            "is_complete": is_complete,
            "is_ongoing": is_ongoing,
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
    lp_rows = lp_result.scalars().all()
    lp_map = {lp.lampa_profile_id: lp for lp in lp_rows}

    # Профили из таймкодов (могут быть не в LampaProfile)
    tc_result = await db.execute(
        select(distinct(Timecode.lampa_profile_id))
        .where(Timecode.device_id == device_id)
    )
    tc_ids = {r[0] for r in tc_result.all()}

    all_ids = sorted((lp_map.keys() | tc_ids) - {""})

    # Кол-во таймкодов для каждого профиля (включая основной "")
    tc_counts: dict[str, int] = {}
    for pid in list(all_ids) + [""]:
        cnt = await db.execute(
            select(func.count()).select_from(Timecode).where(
                Timecode.device_id == device_id,
                Timecode.lampa_profile_id == pid,
            )
        )
        tc_counts[pid] = cnt.scalar() or 0

    # "Основной" (пустой profile_id) доступен если у него есть таймкоды
    # ИЛИ если лимит профилей не исчерпан
    lp_count = len(lp_map)
    limit = settings_cache.get_role_limit(current_user.role, "profile_limit")
    основной_has_tc = "" in tc_ids
    основной_available = основной_has_tc or (limit is None or lp_count < limit)

    def _lp_dict(pid):
        lp = lp_map.get(pid)
        return {
            "profile_id": pid,
            "name": lp.name if lp else "",
            "timecodes_count": tc_counts.get(pid, 0),
            "child": lp.child if lp else False,
            "params": lp.params if lp else {},
        }

    profiles = [_lp_dict(pid) for pid in all_ids]
    if основной_has_tc:
        profiles.insert(0, {"profile_id": "", "name": "Основной", "timecodes_count": tc_counts.get("", 0), "child": False, "params": {}})

    return {"profiles": profiles, "основной_available": основной_available}


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
        limit = settings_cache.get_role_limit(current_user.role, "profile_limit")
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

    limit = settings_cache.get_role_limit(current_user.role, "profile_limit")
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


@router.post("/api/lampa-profile/clear")
async def api_clear_lampa_profile(
    device_id: int = Query(...),
    profile_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет таймкоды профиля, сам профиль не трогает."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)
    result = await db.execute(
        delete(Timecode).where(
            Timecode.device_id == device_id,
            Timecode.lampa_profile_id == profile_id,
        )
    )
    await db.commit()
    return {"ok": True, "deleted": result.rowcount}


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


class _ProfileUpdateBody(BaseModel):
    device_id: int
    profile_id: str
    child: bool | None = None
    params: dict | None = None


@router.patch("/api/lampa-profile")
async def api_update_lampa_profile(
    body: _ProfileUpdateBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Обновляет child и/или params LampaProfile (веб-интерфейс)."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(body.device_id, current_user, db)

    lp = (await db.execute(
        select(LampaProfile).where(
            LampaProfile.device_id == body.device_id,
            LampaProfile.lampa_profile_id == body.profile_id,
        )
    )).scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    if body.child is not None:
        lp.child = body.child
    if body.params is not None:
        lp.params = body.params

    await db.commit()
    return {"ok": True, "child": lp.child, "params": lp.params}


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
    limit = settings_cache.get_role_limit(current_user.role, "profile_limit")
    return {"count": count, "limit": limit}


# ---------------------------------------------------------------------------
# API: детали медиакарточки (TMDB)
# ---------------------------------------------------------------------------

def _mc_to_dict(mc: MediaCard) -> dict:
    movie_item = (
        lampa_hash(mc.original_title)
        if mc.media_type == "movie" and mc.original_title
        else None
    )
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
        "runtime": mc.runtime,
        "episode_run_time": mc.episode_run_time,
        "movie_item": movie_item,
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

    # Для фильмов кэшируем если есть overview и runtime; для сериалов ещё нужен next_ep_air_date и episode_run_time
    if mc and mc.overview and (
        (media_type == "movie" and mc.runtime is not None)
        or (media_type == "tv" and mc.next_ep_air_date is not None and mc.episode_run_time is not None)
    ):
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
        async with httpx.AsyncClient(timeout=20) as client:
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

    date_val = data.get(date_key) or ""
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
        "year": date_val[:4],
        "release_date": date_val,
        "runtime": data.get("runtime"),
    }
    if media_type == "tv":
        seasons = data.get("seasons")
        values["last_air_date"] = data.get("last_air_date") or ""
        values["number_of_seasons"] = data.get("number_of_seasons")
        values["seasons_json"] = json.dumps(seasons, ensure_ascii=False) if seasons else None
        last_ep = data.get("last_episode_to_air") or {}
        values["last_ep_season"] = last_ep.get("season_number")
        values["last_ep_number"] = last_ep.get("episode_number")
        values["next_ep_air_date"] = (data.get("next_episode_to_air") or {}).get("air_date") or ""
        ert = data.get("episode_run_time") or []
        values["episode_run_time"] = ert[0] if ert else 0  # 0 = sentinel (TMDB не знает), NULL = ещё не запрашивали

    stmt = pg_insert(MediaCard).values([values])
    stmt = stmt.on_conflict_do_update(
        index_elements=["card_id"],
        set_={k: stmt.excluded[k] for k in values if k != "card_id"},
    )
    await db.execute(stmt)
    await db.commit()

    orig_title = values.get("original_title") or ""
    movie_item = lampa_hash(orig_title) if media_type == "movie" and orig_title else None
    return {
        "card_id": values["card_id"],
        "tmdb_id": values["tmdb_id"],
        "media_type": values["media_type"],
        "title": values.get("title"),
        "original_title": orig_title,
        "poster_path": values.get("poster_path"),
        "backdrop_path": values.get("backdrop_path"),
        "overview": values.get("overview"),
        "vote_average": values.get("vote_average"),
        "year": values.get("year"),
        "release_date": values.get("release_date"),
        "last_air_date": values.get("last_air_date"),
        "number_of_seasons": values.get("number_of_seasons"),
        "runtime": values.get("runtime"),
        "episode_run_time": values.get("episode_run_time"),
        "movie_item": movie_item,
    }


# ---------------------------------------------------------------------------
# API: актёры карточки
# ---------------------------------------------------------------------------

@router.get("/api/media-card/{card_id}/credits")
async def api_media_card_credits(
    card_id: str,
    current_user: User = Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(status_code=401)

    m = _CARD_ID_RE.match(card_id)
    if not m:
        raise HTTPException(status_code=400)

    tmdb_id, media_type = int(m.group(1)), m.group(2)
    settings = get_settings()
    if not settings.TMDB_TOKEN:
        return {"cast": []}

    headers = {"Authorization": settings.TMDB_TOKEN, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/credits",
                headers=headers,
                params={"language": "ru-RU"},
            )
    except Exception:
        return {"cast": []}

    if resp.status_code != 200:
        return {"cast": []}

    data = resp.json()
    cast = [
        {
            "id": p.get("id"),
            "name": p.get("name") or "",
            "character": p.get("character") or "",
            "profile_path": p.get("profile_path") or "",
        }
        for p in (data.get("cast") or [])[:15]
        if p.get("name")
    ]
    return {"cast": cast}


# ---------------------------------------------------------------------------
# API: рекомендации похожих фильмов/сериалов
# ---------------------------------------------------------------------------

@router.get("/api/media-card/{card_id}/recommendations")
async def api_media_card_recommendations(
    card_id: str,
    current_user: User = Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(status_code=401)

    m = _CARD_ID_RE.match(card_id)
    if not m:
        raise HTTPException(status_code=400)

    tmdb_id, media_type = int(m.group(1)), m.group(2)
    settings = get_settings()
    if not settings.TMDB_TOKEN:
        return {"items": []}

    headers = {"Authorization": settings.TMDB_TOKEN, "Accept": "application/json"}
    base = "https://api.themoviedb.org/3"

    def _parse_items(results: list, mtype: str) -> list:
        out = []
        for r in results:
            title = r.get("title") or r.get("name") or ""
            if not title:
                continue
            date = r.get("release_date") or r.get("first_air_date") or ""
            out.append({
                "id": r.get("id"),
                "media_type": mtype,
                "title": title,
                "year": date[:4] if date else "",
                "poster_path": r.get("poster_path") or "",
            })
        return out

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Параллельно: детали карточки + рекомендации TMDB
            detail_resp, rec_resp = await asyncio.gather(
                client.get(f"{base}/{media_type}/{tmdb_id}", headers=headers, params={"language": "ru-RU"}),
                client.get(f"{base}/{media_type}/{tmdb_id}/recommendations", headers=headers, params={"language": "ru-RU", "page": 1}),
            )

            orig_lang = ""
            genre_ids = []
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                orig_lang = detail.get("original_language") or ""
                genre_ids = [g["id"] for g in (detail.get("genres") or [])]

            rec_results = []
            if rec_resp.status_code == 200:
                rec_results = rec_resp.json().get("results") or []

            # Если русский контент — дополнительно запрашиваем Discover с теми же жанрами
            ru_results = []
            if orig_lang == "ru":
                discover_params = {
                    "language": "ru-RU",
                    "with_original_language": "ru",
                    "sort_by": "popularity.desc",
                    "page": 1,
                    "vote_count.gte": 50,
                }
                if genre_ids:
                    discover_params["with_genres"] = ",".join(str(g) for g in genre_ids[:2])
                disc_resp = await client.get(
                    f"{base}/discover/{media_type}",
                    headers=headers,
                    params=discover_params,
                )
                if disc_resp.status_code == 200:
                    ru_results = disc_resp.json().get("results") or []

    except Exception:
        return {"items": []}

    # Собираем итоговый список: сначала русские (если есть), затем рекомендации
    seen_ids: set = {tmdb_id}  # исключаем саму карточку
    items: list = []

    for r in ru_results:
        if len(items) >= 6:
            break
        if r.get("id") in seen_ids:
            continue
        seen_ids.add(r["id"])
        items.extend(_parse_items([r], media_type))

    for r in rec_results:
        if len(items) >= 10:
            break
        if r.get("id") in seen_ids:
            continue
        seen_ids.add(r["id"])
        items.extend(_parse_items([r], media_type))

    return {"items": items}


# ---------------------------------------------------------------------------
# API: страница актёра — информация + фильмография
# ---------------------------------------------------------------------------

@router.get("/api/actor/{person_id}")
async def api_actor(
    person_id: int,
    current_user: User = Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(status_code=401)

    settings = get_settings()
    if not settings.TMDB_TOKEN:
        raise HTTPException(status_code=503)

    headers = {"Authorization": settings.TMDB_TOKEN, "Accept": "application/json"}
    params  = {"language": "ru-RU"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            person_resp, credits_resp = await asyncio.gather(
                client.get(f"https://api.themoviedb.org/3/person/{person_id}", headers=headers, params=params),
                client.get(f"https://api.themoviedb.org/3/person/{person_id}/combined_credits", headers=headers, params=params),
            )
    except Exception:
        raise HTTPException(status_code=502)

    if person_resp.status_code != 200:
        raise HTTPException(status_code=404)

    person  = person_resp.json()
    credits = credits_resp.json() if credits_resp.status_code == 200 else {}

    cast = credits.get("cast") or []
    # Сортируем по популярности, убираем дубликаты по id
    seen: set = set()
    works = []
    for item in sorted(cast, key=lambda x: x.get("popularity") or 0, reverse=True):
        iid = item.get("id")
        if iid in seen:
            continue
        seen.add(iid)
        media_type = item.get("media_type", "movie")
        title = item.get("title") or item.get("name") or ""
        works.append({
            "id":             iid,
            "media_type":     media_type,
            "title":          title,
            "original_title": item.get("original_title") or item.get("original_name") or "",
            "poster_path":    item.get("poster_path") or "",
            "year":           (item.get("release_date") or item.get("first_air_date") or "")[:4],
            "vote_average":   item.get("vote_average") or 0,
            "character":      item.get("character") or "",
        })

    return {
        "id":           person.get("id"),
        "name":         person.get("name") or "",
        "biography":    person.get("biography") or "",
        "birthday":     person.get("birthday") or "",
        "profile_path": person.get("profile_path") or "",
        "works":        works[:100],
    }


# ---------------------------------------------------------------------------
# API: отметить эпизод просмотренным (percent=100)
# ---------------------------------------------------------------------------

class _MarkWatchedBody(BaseModel):
    device_id: int
    card_id: str
    item: str       # lampa_hash эпизода
    profile_id: str = ""


@router.post("/api/mark-watched")
async def api_mark_watched(
    body: _MarkWatchedBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(body.device_id, current_user, db)

    data = json.dumps({"time": 0, "duration": 0, "percent": 100, "special": True})
    stmt = pg_insert(Timecode).values(
        device_id=body.device_id,
        lampa_profile_id=body.profile_id,
        card_id=body.card_id,
        item=body.item,
        data=data,
    ).on_conflict_do_update(
        constraint="uq_timecode_unique",
        set_={"data": data, "updated_at": func.now()},
    )
    await db.execute(stmt)
    await db.commit()
    return {"ok": True}


@router.get("/api/card-timecodes")
async def api_get_card_timecodes(
    device_id: int = Query(...),
    card_id: str = Query(...),
    profile_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает таймкоды карточки для устройства. Для фильмов — один элемент."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    where = [Timecode.device_id == device_id, Timecode.card_id == card_id]
    if profile_id is not None:
        where.append(Timecode.lampa_profile_id == profile_id)
    result = await db.execute(select(Timecode.item, Timecode.data).where(*where))
    rows = []
    for item, data_raw in result.all():
        try:
            d = json.loads(data_raw)
        except Exception:
            d = {}
        duration_sec = d.get("duration") or None
        rows.append({
            "item": item,
            "percent": d.get("percent", 0),
            "time": d.get("time", 0),
            "duration_sec": duration_sec,
        })
    return rows


@router.get("/api/card-views")
async def api_get_card_views(
    card_id: str = Query(...),
    device_id: int = Query(...),
    profile_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает персональную статистику просмотров карточки для device+profile."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    lampa_profile = profile_id if profile_id is not None else ""

    # Для фильма: SUM(view_count) = сколько раз просмотрен этим профилем
    # Для сериала: COUNT(item WHERE counted_at IS NOT NULL) = кол-во просмотренных серий
    if card_id.endswith("_movie"):
        total_views = (await db.execute(
            select(func.sum(Timecode.view_count)).where(
                Timecode.device_id == device_id,
                Timecode.lampa_profile_id == lampa_profile,
                Timecode.card_id == card_id,
            )
        )).scalar() or 0
        if total_views == 0:
            return {"completed_count": 0}
        return {"completed_count": int(total_views), "media_type": "movie"}

    ep_count = (await db.execute(
        select(func.count()).where(
            Timecode.device_id == device_id,
            Timecode.lampa_profile_id == lampa_profile,
            Timecode.card_id == card_id,
            Timecode.counted_at.isnot(None),
        )
    )).scalar() or 0

    if ep_count == 0:
        return {"completed_count": 0}

    # TV — число серий: episodes (без спецвыпусков) → media_cards → COUNT(items в timecodes)
    mc = (await db.execute(select(MediaCard).where(MediaCard.card_id == card_id))).scalar_one_or_none()
    tmdb_id = mc.tmdb_id if mc else None
    n_ep = 0
    if tmdb_id:
        n_ep = (await db.execute(
            select(func.count()).where(
                Episode.tmdb_show_id == tmdb_id,
                Episode.is_special == False,
            )
        )).scalar() or 0
    if n_ep == 0 and mc:
        n_ep = mc.number_of_episodes or 0
    if n_ep == 0:
        # Фолбэк: реальное кол-во уникальных items в timecodes для этой карточки
        n_ep = (await db.execute(
            select(func.count(func.distinct(Timecode.item))).where(
                Timecode.card_id == card_id,
                Timecode.view_count > 0,
            )
        )).scalar() or 0

    completed_count = round(ep_count / n_ep, 4) if n_ep else None
    return {
        "completed_count": completed_count,
        "media_type": "tv",
        "watched_episodes": ep_count,
        "total_episodes": n_ep or None,
    }


class _SetTimecodeBody(BaseModel):
    device_id: int
    card_id: str
    item: str
    percent: float
    profile_id: str = ""


@router.post("/api/set-timecode")
async def api_set_timecode(
    body: _SetTimecodeBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upsert таймкода с заданным процентом (из веб-интерфейса)."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(body.device_id, current_user, db)

    pct = max(0.0, min(100.0, body.percent))

    # Читаем существующий таймкод чтобы сохранить duration
    where = [
        Timecode.device_id == body.device_id,
        Timecode.card_id == body.card_id,
        Timecode.item == body.item,
        Timecode.lampa_profile_id == body.profile_id,
    ]
    existing = await db.execute(select(Timecode.data).where(*where))
    row = existing.scalar_one_or_none()
    try:
        existing_d = json.loads(row) if row else {}
    except Exception:
        existing_d = {}

    duration = existing_d.get("duration", 0) or 0
    time_sec = round(duration * pct / 100) if duration else 0
    new_data = json.dumps({"time": time_sec, "duration": duration, "percent": pct})

    today = _date.today()
    counted = await _update_card_views(db, body.device_id, body.profile_id, body.card_id, body.item, pct, today)

    stmt = pg_insert(Timecode).values(
        device_id=body.device_id,
        lampa_profile_id=body.profile_id,
        card_id=body.card_id,
        item=body.item,
        data=new_data,
        counted_at=today if counted else None,
        view_count=1 if counted else 0,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_timecode_unique",
        set_={
            "data": new_data,
            "updated_at": func.now(),
            "counted_at": func.coalesce(stmt.excluded.counted_at, Timecode.counted_at),
            "view_count": Timecode.view_count + stmt.excluded.view_count,
        },
    )
    await db.execute(stmt)
    await db.commit()
    await _trim_to_limit(db, body.device_id, body.profile_id, current_user.role)
    return {"ok": True, "percent": pct, "time": time_sec}


@router.delete("/api/episode-timecode")
async def api_delete_episode_timecode(
    device_id: int = Query(...),
    card_id: str = Query(...),
    item: str = Query(...),
    profile_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет таймкод одного эпизода."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    where = [
        Timecode.device_id == device_id,
        Timecode.card_id == card_id,
        Timecode.item == item,
    ]
    if profile_id is not None:
        where.append(Timecode.lampa_profile_id == profile_id)
    await db.execute(delete(Timecode).where(*where))
    await db.commit()
    return {"ok": True}


@router.delete("/api/card-timecodes")
async def api_delete_card_timecodes(
    device_id: int = Query(...),
    card_id: str = Query(...),
    profile_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет все таймкоды карточки для устройства (и опционально профиля)."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(device_id, current_user, db)

    where = [Timecode.device_id == device_id, Timecode.card_id == card_id]
    if profile_id is not None:
        where.append(Timecode.lampa_profile_id == profile_id)
    await db.execute(delete(Timecode).where(*where))
    await db.commit()
    return {"ok": True}


@router.post("/api/unmark-special")
async def api_unmark_special(
    body: _MarkWatchedBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сбрасывает отметку спецэпизода: устанавливает percent=0."""
    if not current_user:
        raise HTTPException(status_code=401)
    await _get_device_or_404(body.device_id, current_user, db)

    data = json.dumps({"time": 0, "duration": 0, "percent": 0})
    stmt = pg_insert(Timecode).values(
        device_id=body.device_id,
        lampa_profile_id=body.profile_id,
        card_id=body.card_id,
        item=body.item,
        data=data,
    ).on_conflict_do_update(
        constraint="uq_timecode_unique",
        set_={"data": data, "updated_at": func.now()},
    )
    await db.execute(stmt)
    await db.commit()
    return {"ok": True}


# ─── check-ongoing ────────────────────────────────────────────────────────────

# Rate limit: раз в сутки на device_id
_check_ongoing_cache: dict[int, _date] = {}


async def _bg_check_ongoing(device_id: int) -> None:
    """Фоновое обновление эпизодов всех сериалов устройства (раз в сутки)."""
    from app.db.database import async_session_maker
    from app.api.episodes import sync_episodes
    from datetime import datetime as _dt, timezone as _tz

    today_start = _dt.combine(_date.today(), _dt.min.time())
    batch_size = settings_cache.get_int("episodes_refresh_batch") or 10
    delay_sec  = settings_cache.get_int("episodes_refresh_delay") or 2

    try:
        async with async_session_maker() as db:
            # Все TV-карточки с таймкодами данного устройства, уже линкованные с MyShows
            # и не обновлявшиеся сегодня
            card_ids_q = (
                select(distinct(Timecode.card_id))
                .where(
                    Timecode.device_id == device_id,
                    Timecode.card_id.like("%_tv"),
                )
            )
            card_ids = (await db.execute(card_ids_q)).scalars().all()

            if not card_ids:
                return

            result = await db.execute(
                select(MediaCard).where(
                    MediaCard.card_id.in_(card_ids),
                    MediaCard.myshows_show_id.isnot(None),
                    (MediaCard.episodes_synced_at == None) |
                    (MediaCard.episodes_synced_at < today_start),
                )
            )
            cards = result.scalars().all()
            logger.info(f"check_ongoing device={device_id}: {len(cards)} shows to refresh")

            async with httpx.AsyncClient(timeout=30) as client:
                for i in range(0, len(cards), batch_size):
                    batch = cards[i:i + batch_size]
                    for mc in batch:
                        try:
                            await sync_episodes(mc, db, client)
                        except Exception as e:
                            logger.warning(f"check_ongoing: {mc.card_id} failed: {e}")
                            await db.rollback()
                    if i + batch_size < len(cards):
                        import asyncio as _aio
                        await _aio.sleep(delay_sec)

    except Exception as e:
        logger.error(f"check_ongoing device={device_id} failed: {e}", exc_info=True)


@router.get("/api/check-ongoing")
async def api_check_ongoing(
    device: Device = Depends(get_device_by_token),
):
    """Fire-and-forget: обновляет эпизоды всех сериалов устройства раз в сутки."""
    if not device:
        raise HTTPException(status_code=401)

    today = _date.today()
    if _check_ongoing_cache.get(device.id) != today:
        _check_ongoing_cache[device.id] = today
        import asyncio
        asyncio.create_task(_bg_check_ongoing(device.id))

    return {"ok": True}
