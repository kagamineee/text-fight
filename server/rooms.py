"""房间管理：口令、房号、限流、生命周期。

房间只活在内存里，不落库、不恢复；服务重启即清空。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .game.engine import Room
from .game.models import Player
from .packs.loader import Pack, load_packs

logger = logging.getLogger("yap.rooms")

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 6
MAX_SEATS = 8
MAX_JOIN_FAILURES = 5
JOIN_LOCK_SECONDS = 600


class RoomError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RoomManager:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.packs: dict[str, Pack] = load_packs()
        self.default_pack_id = next(iter(self.packs))
        self.rooms: dict[str, Room] = {}
        self._hasher = PasswordHasher()
        self._join_failures: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- 限流
    def _check_join_limit(self, source: str) -> None:
        now = time.time()
        failures = [t for t in self._join_failures.get(source, []) if now - t < JOIN_LOCK_SECONDS]
        self._join_failures[source] = failures
        if len(failures) >= MAX_JOIN_FAILURES:
            raise RoomError("LOCKED", "口令错误次数过多，请 10 分钟后再试")

    def _note_join_failure(self, source: str) -> None:
        self._join_failures.setdefault(source, []).append(time.time())

    # ---------------------------------------------------------------- 创建/加入
    def create_room(
        self, host_name: str, password: str, player_id: str | None = None
    ) -> tuple[Room, Player]:
        code = self._new_code()
        room_id = uuid.uuid4().hex[:12]
        password_hash = self._hasher.hash(password)
        room = Room(
            room_id=room_id,
            code=code,
            password_hash=password_hash,
            pack=self.packs[self.default_pack_id],
            host_id="",
            hub=self.hub,
        )
        host = room.add_player(host_name, kind="human", pid=player_id)
        room.host_id = host.player_id
        self.rooms[room_id] = room
        return room, host

    def join_room(
        self,
        code: str,
        password: str,
        name: str,
        source: str,
        player_id: str | None = None,
    ) -> tuple[Room, Player]:
        self._check_join_limit(source)
        room = self.find_by_code(code)
        if room is None:
            raise RoomError("ROOM_NOT_FOUND", "房间不存在")
        try:
            self._hasher.verify(room.password_hash, password)
        except VerifyMismatchError:
            self._note_join_failure(source)
            raise RoomError("AUTH_FAILED", "口令不正确") from None
        except Exception:
            raise RoomError("AUTH_FAILED", "口令校验失败") from None
        if len(room.players) >= MAX_SEATS:
            raise RoomError("ROOM_FULL", "房间已满（上限 8 人）")
        if room.phase.value not in ("lobby", "over"):
            raise RoomError("ALREADY_STARTED", "对局已经开始，无法中途加入")
        player = room.add_player(name, kind="human", pid=player_id)
        return room, player

    def find_by_code(self, code: str) -> Optional[Room]:
        target = code.strip().upper()
        for room in self.rooms.values():
            if room.code == target:
                return room
        return None

    def get(self, room_id: str) -> Optional[Room]:
        return self.rooms.get(room_id)

    def _new_code(self) -> str:
        for _ in range(200):
            code = "".join(
                __import__("random").choice(CODE_CHARS) for _ in range(CODE_LEN)
            )
            if self.find_by_code(code) is None:
                return code
        raise RoomError("INTERNAL", "房号生成失败，请重试")

    # ---------------------------------------------------------------- 大厅操作
    def add_bot(
        self, room: Room, requester: str, difficulty: str = "normal"
    ) -> Player:
        self._require_host(room, requester)
        self._require_lobby(room)
        if len(room.players) >= MAX_SEATS:
            raise RoomError("ROOM_FULL", "房间已满")
        index = sum(1 for p in room.players.values() if p.kind == "bot") + 1
        return room.add_player(
            f"假人-{index}", kind="bot", difficulty=difficulty
        )

    def remove_player(self, room: Room, requester: str, target_id: str) -> None:
        self._require_host(room, requester)
        self._require_lobby(room)
        if target_id == requester:
            raise RoomError("INVALID", "房主不能踢出自己")
        if target_id not in room.players:
            raise RoomError("INVALID", "该玩家不在房间内")
        room.remove_player(target_id)

    def start_game(self, room: Room, requester: str) -> None:
        self._require_host(room, requester)
        self._require_lobby(room)
        if len(room.players) < 2:
            raise RoomError("INVALID", "至少需要 2 个人才能开局")
        waiting = [
            p.name for p in room.players.values() if p.kind == "human" and not p.ready
        ]
        if waiting:
            raise RoomError("NOT_READY", "还有玩家没准备：" + "、".join(waiting))
        for player in room.players.values():
            player.hp = room.pack.hp.base
            player.hp_max = room.pack.hp.base
            player.hp_peak = room.pack.hp.base
            player.alive = True
            player.survived_turns = 0
        room.start()

    def set_ready(self, room: Room, player_id: str, ready: bool) -> None:
        self._require_lobby(room)
        player = room.players.get(player_id)
        if player is None:
            raise RoomError("INVALID", "你不在房间里")
        player.ready = ready

    async def return_lobby(self, room: Room, requester: str) -> None:
        self._require_host(room, requester)
        if room.phase.value == "lobby" and not room.over:
            # 已经在房间状态（例如另一个标签页先点了），直接补发状态即可
            return
        if not room.over:
            raise RoomError("WRONG_PHASE", "这局还没结束")
        await room.reset_for_lobby()

    def _require_host(self, room: Room, requester: str) -> None:
        if room.host_id != requester:
            raise RoomError("NOT_HOST", "只有房主可以执行该操作")

    def _require_lobby(self, room: Room) -> None:
        if room.phase.value != "lobby":
            raise RoomError("WRONG_PHASE", "对局已开始，无法执行该操作")

    # ---------------------------------------------------------------- 清理
    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for room_id, room in list(self.rooms.items()):
                if room.over and room.phase.value == "over":
                    left = getattr(room, "_over_since", None)
                    if left is None:
                        room._over_since = now
                    elif now - left > 24 * 3600:
                        await room.stop()
                        del self.rooms[room_id]
