"""
WebSocket Connection Manager — рассылка обновлений таймкодов между устройствами одного пользователя.
"""
import logging
import uuid
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        # user_id -> {conn_id: WebSocket}
        # conn_id — UUID на каждое соединение, чтобы несколько устройств с одним токеном сосуществовали
        self._connections: dict[int, dict[str, WebSocket]] = defaultdict(dict)

    async def connect(self, user_id: int, ws: WebSocket) -> str:
        """Принимает соединение, возвращает уникальный conn_id."""
        await ws.accept()
        conn_id = str(uuid.uuid4())
        self._connections[user_id][conn_id] = ws
        logger.debug(f"WS connected: user={user_id} conn={conn_id}")
        return conn_id

    def disconnect(self, user_id: int, conn_id: str) -> None:
        user_conns = self._connections.get(user_id, {})
        user_conns.pop(conn_id, None)
        if not user_conns:
            self._connections.pop(user_id, None)
        logger.debug(f"WS disconnected: user={user_id} conn={conn_id}")

    async def broadcast(self, user_id: int, sender_conn_id: str | None, message: dict) -> None:
        """Отправляет сообщение всем соединениям пользователя кроме отправителя."""
        user_conns = self._connections.get(user_id, {})
        logger.info(f"WS broadcast: user={user_id} conns={len(user_conns)} msg_type={message.get('type')}")
        dead: list[str] = []
        for conn_id, ws in list(user_conns.items()):
            if conn_id == sender_conn_id:
                continue
            try:
                await ws.send_json(message)
                logger.info(f"WS broadcast sent: conn={conn_id}")
            except Exception as e:
                logger.warning(f"WS broadcast failed: conn={conn_id} err={e}")
                dead.append(conn_id)
        for c in dead:
            user_conns.pop(c, None)


manager = ConnectionManager()
