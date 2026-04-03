"""
Синхронизация настроек плагинов Lampa между устройствами одного пользователя.

GET  /api/plugin-settings?token=&plugin=&lampa_profile_id=  — получить настройки профиля
PATCH /api/plugin-settings?token=&plugin=&lampa_profile_id= — обновить один ключ {key, value}
WS   /api/plugin-settings/ws?token=                        — real-time канал обновлений

lampa_profile_id по умолчанию '' — настройки без профиля.
WS-сообщения включают lampa_profile_id, клиент применяет только совпадающий профиль.
"""
import json
import logging
from collections import defaultdict
from typing import Dict, Set

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db, async_session_maker
from app.db.models import PluginSettings
from app.api.dependencies import get_device_by_token
from app.db.models import Device

logger = logging.getLogger(__name__)

router = APIRouter()

# user_id -> set of active WebSocket connections
_connections: Dict[int, Set[WebSocket]] = defaultdict(set)


async def _get_or_create(
    db: AsyncSession, user_id: int, lampa_profile_id: str, plugin: str
) -> PluginSettings:
    result = await db.execute(
        select(PluginSettings).where(
            PluginSettings.user_id == user_id,
            PluginSettings.lampa_profile_id == lampa_profile_id,
            PluginSettings.plugin == plugin,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = PluginSettings(
            user_id=user_id,
            lampa_profile_id=lampa_profile_id,
            plugin=plugin,
            settings="{}",
        )
        db.add(row)
        await db.flush()
    return row


async def _broadcast(user_id: int, plugin: str, lampa_profile_id: str, key: str, value) -> None:
    """Отправить обновление всем подключённым устройствам пользователя.
    Клиент применяет только если lampa_profile_id совпадает с текущим профилем.
    """
    msg = json.dumps({"plugin": plugin, "lampa_profile_id": lampa_profile_id, "key": key, "value": value})
    dead = set()
    for ws in list(_connections.get(user_id, set())):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _connections[user_id] -= dead


# ---------------------------------------------------------------------------
# GET — получить настройки плагина для профиля
# ---------------------------------------------------------------------------

@router.get("/api/plugin-settings")
async def get_plugin_settings(
    plugin: str = Query(..., min_length=1, max_length=100),
    lampa_profile_id: str = Query(default="", max_length=100),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    if not device:
        raise HTTPException(status_code=401, detail="Unauthorized")

    row = await _get_or_create(db, device.user_id, lampa_profile_id, plugin)
    await db.commit()

    try:
        data = json.loads(row.settings)
    except Exception:
        data = {}

    return data


# ---------------------------------------------------------------------------
# PATCH — обновить один ключ
# ---------------------------------------------------------------------------

class PatchBody(BaseModel):
    key: str
    value: object


@router.patch("/api/plugin-settings")
async def patch_plugin_settings(
    body: PatchBody,
    plugin: str = Query(..., min_length=1, max_length=100),
    lampa_profile_id: str = Query(default="", max_length=100),
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
):
    if not device:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not body.key:
        raise HTTPException(status_code=400, detail="key required")

    row = await _get_or_create(db, device.user_id, lampa_profile_id, plugin)

    try:
        data = json.loads(row.settings)
    except Exception:
        data = {}

    data[body.key] = body.value
    row.settings = json.dumps(data, ensure_ascii=False)
    await db.commit()

    await _broadcast(device.user_id, plugin, lampa_profile_id, body.key, body.value)

    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket — real-time канал
# ---------------------------------------------------------------------------

@router.websocket("/api/plugin-settings/ws")
async def plugin_settings_ws(
    websocket: WebSocket,
    token: str = Query(None),
):
    async with async_session_maker() as db:
        if not token:
            await websocket.close(code=4001)
            return

        result = await db.execute(
            select(Device).where(Device.token == token)
        )
        device = result.scalar_one_or_none()
        if not device:
            await websocket.close(code=4001)
            return

        user_id = device.user_id

    await websocket.accept()
    _connections[user_id].add(websocket)
    logger.debug("WS plugin-settings: user %s connected (%s total)", user_id, len(_connections[user_id]))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _connections[user_id].discard(websocket)
        logger.debug("WS plugin-settings: user %s disconnected", user_id)
