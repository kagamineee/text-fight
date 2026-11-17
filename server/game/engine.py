"""对局引擎：回合状态机 + 事件记账 + 广播。

设计要点：
- 四阶段 DECLARE → ADJUDICATE → SETTLE → NEXT，时限全部由服务端驱动。
- 玩家的输入只有一段自由文本；效果与目标由裁判判断。
- 受到伤害时没有即时回应，直接按当前文本与属性结算。
- 一切状态变更都经过 _record（写事件）与内存投影，保证事件溯源可重建。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from . import attributes as attr_mod
from . import judge
from .bots import BotBrain
from .models import (
    Declaration,
    LedgerEntry,
    LedgerType,
    Phase,
    Player,
)
from ..packs.loader import Pack

logger = logging.getLogger("yap.engine")

BOT_THINK_MIN = 1.0
BOT_THINK_MAX = 2.2


class Collector:
    """阶段收集器：等待"所有符合条件的人都提交"或超时。"""

    def __init__(self) -> None:
        self.items: dict[str, object] = {}
        self._cond = asyncio.Condition()

    def reset(self) -> None:
        self.items = {}

    async def submit(self, key: str, value: object) -> None:
        async with self._cond:
            self.items[key] = value
            self._cond.notify_all()

    async def wait(self, predicate, timeout: float) -> dict:
        try:
            async with asyncio.timeout(timeout):
                async with self._cond:
                    await self._cond.wait_for(lambda: predicate(self.items))
        except TimeoutError:
            pass
        return dict(self.items)


def _now_ms() -> float:
    return time.time() * 1000


class Room:
    def __init__(
        self,
        *,
        room_id: str,
        code: str,
        password_hash: str,
        pack: Pack,
        host_id: str,
        store,
        hub,
    ) -> None:
        self.room_id = room_id
        self.code = code
        self.password_hash = password_hash
        self.pack = pack
        self.host_id = host_id
        self.store = store
        self.hub = hub

        self.players: dict[str, Player] = {}
        self.order: list[str] = []
        self.ledger: list[LedgerEntry] = []
        self.seq = 0
        self.turn_no = 0
        self.phase = Phase.LOBBY
        self.over = False
        self.winner: Optional[str] = None
        self.current_actor: Optional[str] = None
        self.deadline: Optional[float] = None
        self.bot_brains: dict[str, BotBrain] = {}

        self._collector = Collector()
        self._runner: Optional[asyncio.Task] = None
        self._closed = False

    # ------------------------------------------------------------------ 玩家
    def add_player(self, name: str, kind: str = "human", pid: str | None = None) -> Player:
        pid = pid or uuid.uuid4().hex[:12]
        seat = len(self.order)
        player = Player(
            player_id=pid,
            name=name,
            seat=seat,
            kind=kind,
            hp=self.pack.hp.base,
            hp_max=self.pack.hp.base,
            appeals_left=self.pack.appeal.count,
            hp_peak=self.pack.hp.base,
            ready=(kind == "bot"),
        )
        self.players[pid] = player
        self.order.append(pid)
        if kind == "bot":
            self.bot_brains[pid] = BotBrain(seed=hash(pid) & 0xFFFF, preset_index=seat)
        self._record(
            LedgerType.SYSTEM,
            f"{name} 加入了房间。",
            actor=pid,
            payload={
                "kind": "player_joined",
                "player_id": pid,
                "name": name,
                "seat": seat,
                "player_kind": kind,
                "hp": player.hp,
                "hp_max": player.hp_max,
            },
        )
        return player

    def remove_player(self, pid: str) -> None:
        if pid not in self.players:
            return
        name = self.players[pid].name
        del self.players[pid]
        self.order = [p for p in self.order if p != pid]
        self.bot_brains.pop(pid, None)
        for i, other in enumerate(self.order):
            self.players[other].seat = i
        self._record(
            LedgerType.SYSTEM,
            f"{name} 离开了房间。",
            payload={"kind": "player_left", "player_id": pid},
        )

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    # ------------------------------------------------------------------ 记账
    def _record(
        self,
        type_: LedgerType,
        content: str,
        *,
        actor: Optional[str] = None,
        intensity: Optional[float] = None,
        payload: Optional[dict] = None,
        status: str = "active",
    ) -> LedgerEntry:
        data = dict(payload or {})
        data.setdefault("kind", type_.value)
        ev = self.store.append(
            self.room_id,
            self.turn_no,
            actor,
            type_.value,
            content,
            intensity,
            data,
            status,
        )
        entry = LedgerEntry(
            seq=ev.seq,
            turn_no=ev.turn_no,
            actor=ev.actor,
            type=ev.type,
            content=ev.content,
            intensity=ev.intensity,
            status=ev.status,
            payload=data,
        )
        self.ledger.append(entry)
        self.seq = ev.seq
        return entry

    # ------------------------------------------------------------------ 广播
    async def _broadcast(self, msg: dict) -> None:
        msg["seq"] = self.seq
        msg["server_now"] = _now_ms()
        await self.hub.broadcast_room(self.room_id, msg)

    async def _send(self, pid: str, msg: dict) -> None:
        msg["seq"] = self.seq
        msg["server_now"] = _now_ms()
        await self.hub.send_player(pid, msg)

    def _deadline(self, seconds: float) -> float:
        self.deadline = time.time() + seconds
        return self.deadline

    async def broadcast_state(self) -> None:
        await self._broadcast(self.room_state())

    # ------------------------------------------------------------------ 视图
    def room_state(self) -> dict:
        return {
            "type": "room_state",
            "room_id": self.room_id,
            "code": self.code,
            "phase": self.phase.value,
            "host_id": self.host_id,
            "pack": self.pack.public_view(),
            "players": [
                self.players[pid].public_view() for pid in self.order if pid in self.players
            ],
            "turn_no": self.turn_no,
            "current_actor": self.current_actor,
            "deadline": self.deadline,
            "over": self.over,
        }

    def snapshot_for(self, pid: str) -> dict:
        me = self.players.get(pid)
        privates = {}
        if me is not None:
            privates = {
                "attributes": [a.private_view() for a in me.attributes.values()]
            }
        return {
            "type": "snapshot",
            "state": self.room_state(),
            "private": privates,
            "ledger": [e.view() for e in self.ledger[-200:]],
            "last_seq": self.seq,
        }

    # ------------------------------------------------------------------ 生命周期
    def start(self, resume: bool = False) -> None:
        if self._runner and not self._runner.done():
            return
        self._closed = False
        if not resume:
            self.turn_no = 0
            self._record(
                LedgerType.SYSTEM,
                "对局开始。",
                payload={"kind": "game_started"},
            )
        self.phase = Phase.DECLARE
        self._runner = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._closed = True
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass

    async def reset_for_lobby(self) -> None:
        """一局打完，清场回到准备阶段。"""
        await self.stop()
        self._closed = False
        self._runner = None
        self.over = False
        self.winner = None
        self.turn_no = 0
        self.phase = Phase.LOBBY
        self.current_actor = None
        self.deadline = None
        self.ledger = []
        for player in self.players.values():
            player.hp = self.pack.hp.base
            player.hp_max = self.pack.hp.base
            player.hp_peak = self.pack.hp.base
            player.alive = True
            player.survived_turns = 0
            player.attributes.clear()
            player.appeals_left = self.pack.appeal.count
            player.ready = player.kind == "bot"
        self._record(
            LedgerType.SYSTEM,
            "回到房间。",
            payload={"kind": "lobby_returned"},
        )

    # ------------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        try:
            while not self._closed:
                alive = self.alive_players()
                if len(alive) <= 1:
                    await self._finish(alive)
                    return
                await self._play_turn(alive)
                if self.over:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("房间 %s 主循环异常终止", self.room_id)
            self.phase = Phase.OVER

    def _next_actor(self, alive: list[Player]) -> Player:
        if self.current_actor is None:
            start = 0
        else:
            prev = self.players.get(self.current_actor)
            start = (prev.seat + 1) if prev else 0
        n = len(self.order)
        for offset in range(n):
            pid = self.order[(start + offset) % n]
            player = self.players.get(pid)
            if player and player.alive:
                return player
        return alive[0]

    async def _play_turn(self, alive: list[Player]) -> None:
        self.turn_no += 1
        for p in alive:
            p.survived_turns += 1
            if self.turn_no > 1:
                p.hp = min(p.hp_max, p.hp + self.pack.hp.regen)
            p.hp_peak = max(p.hp_peak, p.hp)

        actor = self._next_actor(alive)
        self.current_actor = actor.player_id
        self.phase = Phase.DECLARE
        self._collector.reset()
        self._record(
            LedgerType.SYSTEM,
            f"第 {self.turn_no} 回合，轮到 {actor.name}。",
            payload={
                "kind": "turn_begin",
                "turn_no": self.turn_no,
                "current_player": actor.player_id,
                "hp": {p.player_id: p.hp for p in self.players.values() if p.alive},
            },
        )
        deadline = self._deadline(self.pack.timers.declare_s)
        await self._broadcast(
            {
                "type": "turn_begin",
                "turn_no": self.turn_no,
                "current_player": actor.player_id,
                "hp": {p.player_id: p.hp for p in alive},
            }
        )
        await self._broadcast(
            {"type": "phase_change", "phase": Phase.DECLARE.value, "deadline": deadline,
             "current_actor": actor.player_id}
        )
        self._spawn_bots(actor)

        await self._collector.wait(lambda items: actor.player_id in items, self.pack.timers.declare_s)
        raw = self._collector.items.get(actor.player_id)
        decl: Declaration
        if isinstance(raw, Declaration):
            decl = raw
        else:
            decl = Declaration(
                actor_id=actor.player_id,
                content="（时间到了，没来得及动手。）",
            )

        self._record(
            LedgerType.DECLARATION,
            decl.content,
            actor=decl.actor_id,
            payload={"content": decl.content},
        )
        await self._broadcast(
            {
                "type": "declaration",
                "turn_no": self.turn_no,
                "actor": decl.actor_id,
                "actor_name": actor.name,
                "content": decl.content,
            }
        )

        await self._adjudicate(actor, decl)

        if not self.alive_players():
            await self._finish([])

    async def _adjudicate(self, actor: Player, decl: Declaration) -> None:
        self.phase = Phase.ADJUDICATE
        deadline = self._deadline(self.pack.timers.judge_s)
        await self._broadcast(
            {"type": "phase_change", "phase": Phase.ADJUDICATE.value, "deadline": deadline}
        )

        verdict = judge.adjudicate(self.players, self.turn_no, self.pack, decl)

        # 属性先落地（新建 / 变化），只给本人回执
        attr_updates: list[dict] = []
        for change in verdict.attr_changes:
            owner = self.players.get(change.get("owner"))
            if owner is None:
                continue
            if change.get("remove"):
                attr_id = str(change.get("attr_id"))
                attr = owner.attributes.pop(attr_id, None)
                if attr is None:
                    continue
                attr_updates.append(
                    {
                        "owner": owner.player_id,
                        "attr_id": attr_id,
                        "name": attr.name,
                        "remove": True,
                    }
                )
                await self._send(
                    owner.player_id,
                    {"type": "attribute_private", "notice": "删掉了", "attr": attr.private_view()},
                )
                continue
            if change.get("created"):
                if len(owner.attributes) >= self.pack.attributes.max_count:
                    continue
                if any(a.name == change.get("name") for a in owner.attributes.values()):
                    continue
                attr = attr_mod.create_attribute(
                    owner_id=owner.player_id,
                    name=str(change.get("name", "")),
                    attr_type=str(change.get("attr_type", "utility")),
                    value=int(change.get("value", 1)),
                    cap=int(change.get("cap", 2)),
                    narrative=str(change.get("narrative", "")),
                    turn_no=self.turn_no,
                    max_cap=self.pack.attributes.max_cap,
                )
                owner.attributes[attr.attr_id] = attr
                self._record(
                    LedgerType.ATTRIBUTE_OPEN,
                    f"{owner.name} 有了「{attr.name}」。",
                    actor=owner.player_id,
                    payload={"kind": "attribute_sealed", **attr_mod.seal_event(attr)},
                )
                await self._broadcast(
                    {
                        "type": "attribute_opened",
                        "player_id": owner.player_id,
                        "player_name": owner.name,
                        "name": attr.name,
                        "narrative": attr.narrative,
                        "attr_type": attr.attr_type,
                    }
                )
                await self._send(
                    owner.player_id,
                    {"type": "attribute_private", "notice": "新属性", "attr": attr.private_view()},
                )
                continue

            attr = owner.attributes.get(str(change.get("attr_id")))
            if attr is None:
                continue
            attr.value = max(0, min(attr.cap, int(change.get("value", attr.value))))
            attr_updates.append(
                {
                    "owner": owner.player_id,
                    "attr_id": attr.attr_id,
                    "name": attr.name,
                    "value": attr.value,
                }
            )
            await self._send(
                owner.player_id,
                {"type": "attribute_private", "notice": "变化", "attr": attr.private_view()},
            )

        # 伤害 / 治疗
        applied: list[dict] = []
        deaths: list[tuple[str, str]] = []
        for effect in verdict.effects:
            pid = str(effect.get("player_id"))
            target = self.players.get(pid)
            if target is None or not target.alive:
                continue
            delta = int(effect.get("hp_delta", 0))
            before = target.hp
            target.hp = max(0, min(target.hp_max, before + delta))
            actual = target.hp - before
            if actual == 0:
                continue
            applied.append({"player_id": pid, "hp_delta": actual, "hp": target.hp})
            if target.hp == 0 and actual < 0:
                target.alive = False
                deaths.append((pid, actor.player_id))

        self.phase = Phase.SETTLE
        self.deadline = None
        await self._broadcast(
            {"type": "phase_change", "phase": Phase.SETTLE.value, "deadline": None}
        )

        entry = self._record(
            LedgerType.ADJUDICATION,
            verdict.narrative,
            actor=actor.player_id,
            payload={
                "effects": applied,
                "attr_changes": attr_updates,
            },
        )

        await self._broadcast(
            {
                "type": "adjudication",
                "ledger_seq": entry.seq,
                "narrative": verdict.narrative,
                "effects": [
                    {
                        "player_id": item["player_id"],
                        "delta": item["hp_delta"],
                        "hp": item["hp"],
                    }
                    for item in applied
                ],
            }
        )

        for item in applied:
            await self._broadcast(
                {
                    "type": "hp_update",
                    "player_id": item["player_id"],
                    "hp": item["hp"],
                    "hp_max": self.players[item["player_id"]].hp_max,
                    "delta": item["hp_delta"],
                }
            )

        for pid, killer in deaths:
            victim = self.players[pid]
            self._record(
                LedgerType.DEATH,
                f"{victim.name} 出局。",
                actor=pid,
                payload={"player_id": pid, "killer": killer, "cause": "combat"},
            )
            await self._broadcast(
                {
                    "type": "player_dead",
                    "player_id": pid,
                    "name": victim.name,
                    "cause": "combat",
                    "killer": killer,
                }
            )

        await self.broadcast_state()

    async def _finish(self, alive: list[Player]) -> None:
        self.over = True
        self.phase = Phase.OVER
        self.deadline = None
        winner = alive[0] if len(alive) == 1 else None
        self.winner = winner.player_id if winner else None
        ranking = sorted(
            self.players.values(),
            key=lambda p: (p.alive, p.hp, p.survived_turns),
            reverse=True,
        )
        self._record(
            LedgerType.SYSTEM,
            f"{winner.name} 撑到了最后。" if winner else "全倒了，没人站着。",
            payload={
                "kind": "game_over",
                "winner": self.winner,
                "ranking": [
                    {
                        "player_id": p.player_id,
                        "name": p.name,
                        "alive": p.alive,
                        "hp": p.hp,
                        "survived_turns": p.survived_turns,
                    }
                    for p in ranking
                ],
            },
        )
        await self._broadcast(
            {
                "type": "game_over",
                "winner": self.winner,
                "winner_name": winner.name if winner else None,
                "ranking": [
                    {
                        "player_id": p.player_id,
                        "name": p.name,
                        "alive": p.alive,
                        "hp": p.hp,
                        "survived_turns": p.survived_turns,
                    }
                    for p in ranking
                ],
            }
        )

    # ------------------------------------------------------------------ 假人
    def _spawn_bots(self, actor: Player) -> None:
        bot = self.players.get(actor.player_id)
        brain = self.bot_brains.get(actor.player_id)
        if bot is None or brain is None or not bot.alive:
            return
        asyncio.create_task(self._bot_declare(bot, brain))

    async def _bot_declare(self, bot: Player, brain: BotBrain) -> None:
        await asyncio.sleep(
            BOT_THINK_MIN + (BOT_THINK_MAX - BOT_THINK_MIN) * brain.rng.random()
        )
        if self.phase != Phase.DECLARE or self.current_actor != bot.player_id:
            return
        decl = brain.declare(bot, self.players, self.turn_no)
        await self._collector.submit(bot.player_id, decl)

    # ------------------------------------------------------------------ 玩家动作
    async def submit_declare(self, pid: str, decl: Declaration) -> None:
        if self.phase != Phase.DECLARE:
            raise ValueError("还没到你的回合")
        if pid != self.current_actor:
            raise ValueError("还没轮到你")
        actor = self.players.get(pid)
        if actor is None or not actor.alive:
            raise ValueError("你已出局")
        if not decl.content.strip():
            raise ValueError("写点什么再发")
        await self._collector.submit(pid, decl)

    async def open_attribute(self, pid: str, payload: dict) -> dict:
        """协议兼容入口。正常对局里属性只由裁判改动。"""
        if self.over:
            raise ValueError("对局已结束")
        allowed = self.phase == Phase.SETTLE or (
            self.phase == Phase.DECLARE and self.current_actor == pid
        )
        if not allowed:
            raise ValueError("现在不能开户")
        player = self.players.get(pid)
        if player is None or not player.alive:
            raise ValueError("你已出局")

        attr_mod.validate_open(player, self.pack, payload)
        attr = attr_mod.build_attribute(player, payload, self.turn_no)
        player.attributes[attr.attr_id] = attr
        self._record(
            LedgerType.ATTRIBUTE_OPEN,
            f"{player.name} 有了「{attr.name}」。",
            actor=pid,
            payload={"kind": "attribute_sealed", **attr_mod.seal_event(attr)},
        )
        await self._broadcast(
            {
                "type": "attribute_opened",
                "player_id": pid,
                "player_name": player.name,
                "name": attr.name,
                "narrative": attr.narrative,
                "attr_type": attr.attr_type,
            }
        )
        await self._send(
            pid, {"type": "attribute_private", "notice": "新属性", "attr": attr.private_view()}
        )
        return attr.private_view()

    async def chat(self, pid: str, text: str) -> None:
        player = self.players.get(pid)
        if player is None:
            raise ValueError("你不在房间内")
        self._record(
            LedgerType.SYSTEM,
            f"{player.name}：{text}",
            actor=pid,
            payload={"kind": "chat", "channel": "table", "text": text},
        )
        await self._broadcast(
            {
                "type": "chat",
                "channel": "table",
                "actor": pid,
                "name": player.name,
                "text": text,
            }
        )
