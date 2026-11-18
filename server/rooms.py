"""房间管理：口令、房号、限流、生命周期。

房间只活在内存里，不落库、不恢复；服务重启即清空。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .game.engine import Room
from .game.models import Player
from .packs.loader import Pack, PackRegistry

logger = logging.getLogger("yap.rooms")

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 6
MAX_SEATS = 8
MAX_JOIN_FAILURES = 5
JOIN_LOCK_SECONDS = 600
IDLE_ROOM_SECONDS = 30 * 60


class RoomError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RoomManager:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.registry = PackRegistry().reload()
        self.packs: dict[str, Pack] = self.registry.packs
        self.default_pack_id = self.registry.default_id
        self.rooms: dict[str, Room] = {}
        self._hasher = PasswordHasher()
        self._join_failures: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- 扩展包
    def refresh_packs(self) -> bool:
        """包目录有变化时重新扫描；返回是否真的重载过。"""
        changed = self.registry.maybe_reload()
        if changed:
            self.packs = self.registry.packs
            if self.default_pack_id not in self.packs:
                self.default_pack_id = self.registry.default_id
        return changed

    def reload_packs(self) -> dict:
        """强制重扫（供 /api/packs/reload 使用），不影响已创建的房间。"""
        self.registry.reload()
        self.packs = self.registry.packs
        self.default_pack_id = self.registry.default_id
        return {
            "packs": [p.public_view() for p in self.packs.values()],
            "issues": self.registry.issues,
            "default": self.default_pack_id,
        }

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
        self,
        host_name: str,
        password: str,
        player_id: str | None = None,
        pack_id: str | None = None,
    ) -> tuple[Room, Player]:
        pack_id = (pack_id or "").strip() or self.default_pack_id
        pack = self.packs.get(pack_id)
        if pack is None:
            raise RoomError("PACK_NOT_FOUND", "该扩展包不存在，请刷新房间列表后重试")
        code = self._new_code()
        room_id = uuid.uuid4().hex[:12]
        password_hash = self._hasher.hash(password)
        room = Room(
            room_id=room_id,
            code=code,
            password_hash=password_hash,
            pack=pack,
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
        room.reassign_host()
        room._idle_since = None
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
        names = {p.name for p in room.players.values()}
        index = 1
        while f"假人-{index}" in names:
            index += 1
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
            p.name for p in room.players.values()
            if p.kind == "human" and (not p.ready or not p.connected)
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
    async def cleanup_once(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for room_id, room in list(self.rooms.items()):
            occupied = any(p.kind == "human" and p.connected for p in room.players.values())
            if occupied:
                room._idle_since = None
            elif room._idle_since is None:
                room._idle_since = now
            expired = (room.over and room._over_since is not None
                       and now - room._over_since >= 24 * 3600)
            idle = room._idle_since is not None and now - room._idle_since >= IDLE_ROOM_SECONDS
            if not room.players or expired or idle:
                await room.stop()
                await self.hub.drop_room(room_id)
                del self.rooms[room_id]
        for source, failures in list(self._join_failures.items()):
            recent = [stamp for stamp in failures if now - stamp < JOIN_LOCK_SECONDS]
            if recent:
                self._join_failures[source] = recent
            else:
                self._join_failures.pop(source, None)
