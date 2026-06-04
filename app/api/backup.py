"""Резервная копия пользовательских данных.

Экспортирует устройства (с профилями и таймкодами) и настройки плагинов текущего
пользователя в JSON. Формат полностью совместим с импортом нового сервиса
(movies-go, POST /api/import): version=1, devices[].profiles/timecodes,
plugin_settings[]. Поле lampa_profile_id отдаётся под именем profile_id/id —
как ожидает импортёр.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import User, Device, LampaProfile, Timecode, PluginSettings
from app.api.dependencies import get_current_user

router = APIRouter()


@router.get("/api/export")
async def export_backup(
    current_user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Скачивание резервной копии данных текущего пользователя (JSON-файл)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="unauthorized")

    out_devices: list[dict] = []

    devices = (
        await db.execute(
            select(Device)
            .where(Device.user_id == current_user.id)
            .order_by(Device.created_at, Device.id)
        )
    ).scalars().all()

    for dev in devices:
        # Профили устройства
        profiles = (
            await db.execute(
                select(LampaProfile)
                .where(LampaProfile.device_id == dev.id)
                .order_by(LampaProfile.id)
            )
        ).scalars().all()

        out_profiles: list[dict] = []
        for p in profiles:
            prof: dict = {
                "id": p.lampa_profile_id,
                "name": p.name,
                "child": p.child,
                "params": p.params if p.params else {},
            }
            if p.icon is not None:
                prof["icon"] = p.icon
            if p.favorite is not None:
                prof["favorite"] = p.favorite
            out_profiles.append(prof)

        # Таймкоды устройства
        timecodes = (
            await db.execute(
                select(Timecode)
                .where(Timecode.device_id == dev.id)
                .order_by(Timecode.id)
            )
        ).scalars().all()

        out_timecodes: list[dict] = []
        for t in timecodes:
            tc: dict = {
                "profile_id": t.lampa_profile_id,
                "card_id": t.card_id,
                "item": t.item,
                "data": t.data,
            }
            if t.view_count:
                tc["view_count"] = t.view_count
            if t.counted_at is not None:
                tc["counted_at"] = t.counted_at.isoformat()
            out_timecodes.append(tc)

        created = dev.created_at or datetime.now(timezone.utc)
        out_devices.append({
            "name": dev.name,
            "token": dev.token,
            "created_at": created.isoformat(),
            "profiles": out_profiles,
            "timecodes": out_timecodes,
        })

    # Настройки плагинов (на пользователя, а не на устройство)
    plugin_settings = (
        await db.execute(
            select(PluginSettings).where(PluginSettings.user_id == current_user.id)
        )
    ).scalars().all()

    out_plugin_settings = [
        {
            "profile_id": s.lampa_profile_id,
            "plugin": s.plugin,
            "settings": s.settings,
        }
        for s in plugin_settings
    ]

    data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "devices": out_devices,
        "plugin_settings": out_plugin_settings,
    }

    payload = json.dumps(data, ensure_ascii=False)
    filename = f"movies-backup-{datetime.now().strftime('%Y-%m-%d')}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
