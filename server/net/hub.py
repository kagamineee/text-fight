"""WebSocket 连接中枢：负责"谁在哪个房间"和消息发送。

刻意不反向依赖 Room，避免循环引用。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger("yap.hub")


class Hub:
    def __init__(self) -> None:
        self._sockets: dict[str, WebSocket] = {}
        self._rooms: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def bind(self, player_id: str, room_id: str | None, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[player_id] = ws
            if room_id:
                self._rooms.setdefault(room_id, set()).add(player_id)

    async def unbind(self, player_id: str, room_id: str | None) -> None:
        async with self._lock:
            self._sockets.pop(player_id, None)
            if room_id and room_id in self._rooms:
                self._rooms[room_id].discard(player_id)

    async def move(self, player_id: str, room_id: str) -> None:
        async with self._lock:
            self._rooms.setdefault(room_id, set()).add(player_id)

    def members(self, room_id: str) -> set[str]:
        return set(self._rooms.get(room_id, set()))

    async def send_player(self, player_id: str, msg: dict) -> None:
        ws = self._sockets.get(player_id)
        if ws is None:
            return
        try:
            await ws.send_json(msg)
        except Exception:
            logger.debug("发送失败，玩家 %s 可能已断开", player_id, exc_info=True)

    async def broadcast_room(self, room_id: str, msg: dict) -> None:
        for pid in list(self.members(room_id)):
            await self.send_player(pid, msg)
