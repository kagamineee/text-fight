"""房间管理：口令、房号、限流、生命周期。

P0 的房间身份只活在内存 + 事件日志；重启由 RoomManager 依据事件重建。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .game.bots import BotBrain
from .game.engine import Room
from .game.models import Attribute, LedgerEntry, Phase, Player
from .packs.loader import Pack, load_packs
from .store.replay import rebuild
from .store.store import Store

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
    def __init__(self, data_dir: Path, hub) -> None:
        self.store = Store(data_dir / "arena.db")
        self.hub = hub
        self.packs: dict[str, Pack] = load_packs()
        self.default_pack_id = next(iter(self.packs))
        self.rooms: dict[str, Room] = {}
        self._hasher = PasswordHasher()
        self._join_failures: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- 启动恢复
    async def restore(self) -> None:
        restored = 0
        for room_id in self.store.list_room_ids():
            meta = self.store.room_meta(room_id)
            if meta is None or meta["closed_at"] is not None:
                continue
            pack = self.packs.get(meta["pack_id"])
            if pack is None:
                logger.warning("房间 %s 的扩展包 %s 不存在，跳过恢复", room_id, meta["pack_id"])
                continue
            events = self.store.events(room_id)
            if not events:
                continue
            state = rebuild(events)
            room = Room(
                room_id=room_id,
                code=meta["code"],
                password_hash=meta["password_hash"],
                pack=pack,
                host_id=meta["host_player_id"],
                store=self.store,
                hub=self.hub,
            )
            room.turn_no = state["turn_no"]
            room.over = state["over"]
            room.winner = state["winner"]
            room.current_actor = state.get("current_actor")
            for pid in state["order"]:
                data = state["players"][pid]
                player = Player(
                    player_id=pid,
                    name=data["name"],
                    seat=data["seat"],
                    kind=data["kind"],
                    hp=data["hp"],
                    hp_max=data["hp_max"],
                    alive=data["alive"],
                    connected=False,
                    ready=False,
                    hp_peak=data["hp_peak"],
                    survived_turns=data["survived_turns"],
                )
                for attr in data["attributes"].values():
                    player.attributes[attr["attr_id"]] = Attribute(**attr)
                room.players[pid] = player
                room.order.append(pid)
                if data["kind"] == "bot":
                    room.bot_brains[pid] = BotBrain(
                        seed=hash(pid) & 0xFFFF, preset_index=data["seat"]
                    )
            room.ledger = [
                LedgerEntry(
                    seq=ev.seq,
                    turn_no=ev.turn_no,
                    actor=ev.actor,
                    type=ev.type,
                    content=ev.content,
                    intensity=ev.intensity,
                    status=ev.status,
                    payload=ev.payload,
                )
                for ev in events[_last_reset_index(events):]
            ]
            room.seq = events[-1].seq
            self.rooms[room_id] = room
            restored += 1
            if state.get("started") and not room.over:
                room.phase = Phase.DECLARE
                room.start(resume=True)
        if restored:
            logger.info("已从事件日志恢复 %d 个房间", restored)

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
            store=self.store,
            hub=self.hub,
        )
        host = room.add_player(host_name, kind="human", pid=player_id)
        room.host_id = host.player_id
        self.store.create_room(room_id, code, password_hash, room.pack.id, host.player_id)
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
    def add_bot(self, room: Room, requester: str) -> Player:
        self._require_host(room, requester)
        self._require_lobby(room)
        if len(room.players) >= MAX_SEATS:
            raise RoomError("ROOM_FULL", "房间已满")
        index = sum(1 for p in room.players.values() if p.kind == "bot") + 1
        return room.add_player(f"假人-{index}", kind="bot")

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
                        self.store.close_room(room_id)
                        del self.rooms[room_id]


def _last_reset_index(events: list) -> int:
    """同房间可能连打多局，对局情况只保留最近一局。"""
    start = 0
    for i, ev in enumerate(events):
        if (ev.payload or {}).get("kind") == "lobby_returned":
            start = i + 1
    return start
