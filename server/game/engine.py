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
import os
import time
import uuid
from typing import Optional

from . import attributes as attr_mod
from . import debug_log  # [TEMP-LOG] 临时调试日志，稳定后与调用点一起删除
from . import judge_ai, summary
from .bots import BotBrain
from .formulas import apply_armor_wear, effective_defense, resolve_damage
from .models import (
    Attribute,
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
        hub,
    ) -> None:
        self.room_id = room_id
        self.code = code
        self.password_hash = password_hash
        self.pack = pack
        self.host_id = host_id
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

        self.judge_base_url = os.environ.get(
            "YAP_JUDGE_URL", judge_ai.DEFAULT_AGENT_URL
        )
        self.judge_model = os.environ.get("YAP_JUDGE_MODEL", "")
        self.world_summary = ""
        self.summary_turn = 0
        self._summary_task: Optional[asyncio.Task] = None

        self._collector = Collector()
        self._runner: Optional[asyncio.Task] = None
        self._closed = False

    # ------------------------------------------------------------------ 玩家
    def add_player(
        self,
        name: str,
        kind: str = "human",
        pid: str | None = None,
        difficulty: str = "normal",
    ) -> Player:
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
            bot_difficulty=difficulty if kind == "bot" else "",
        )
        self.players[pid] = player
        self.order.append(pid)
        if kind == "bot":
            self.bot_brains[pid] = BotBrain(
                seed=hash(pid) & 0xFFFF, difficulty=difficulty
            )
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
                "bot_difficulty": player.bot_difficulty,
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
        self.seq += 1
        entry = LedgerEntry(
            seq=self.seq,
            turn_no=self.turn_no,
            actor=actor,
            type=type_.value,
            content=content,
            intensity=intensity,
            status=status,
            payload=data,
        )
        self.ledger.append(entry)
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

    def _ranking(self) -> list[Player]:
        return sorted(
            self.players.values(),
            key=lambda p: (p.alive, p.hp, p.survived_turns),
            reverse=True,
        )

    def result_view(self) -> Optional[dict]:
        """结算视图：刷新或断线重连后仍能还原胜者与排名。"""
        if not self.over:
            return None
        winner = self.players.get(self.winner) if self.winner else None
        return {
            "winner_name": winner.name if winner else None,
            "ranking": [
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "alive": p.alive,
                    "hp": p.hp,
                    "survived_turns": p.survived_turns,
                }
                for p in self._ranking()
            ],
        }

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
            "result": self.result_view(),
            "world_summary": self.world_summary,
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
        if self._summary_task and not self._summary_task.done():
            self._summary_task.cancel()
        self._summary_task = None
        self.world_summary = ""
        self.summary_turn = 0
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

    async def _expire_attributes(self, alive: list[Player]) -> None:
        """清理到期的限时属性（例如"持续 2 回合"的护盾）。"""
        expired: list[tuple[Player, Attribute]] = []
        for p in alive:
            for attr in list(p.attributes.values()):
                if attr.expires_turn and self.turn_no > attr.expires_turn:
                    expired.append((p, attr))
        if not expired:
            return
        for p, attr in expired:
            p.attributes.pop(attr.attr_id, None)
            self._record(
                LedgerType.SYSTEM,
                f"{p.name} 的「{attr.name}」到时限，消失了。",
                payload={
                    "kind": "attribute_expired",
                    "owner_id": p.player_id,
                    "attr_id": attr.attr_id,
                    "name": attr.name,
                },
            )
            await self._broadcast(
                {
                    "type": "attribute_removed",
                    "player_id": p.player_id,
                    "player_name": p.name,
                    "name": attr.name,
                    "reason": "expired",
                }
            )
        await self.broadcast_state()

    async def _run_per_turn(self, alive: list[Player]) -> list[Player]:
        """周期效果结算：带 per_turn 的属性（中毒、灼烧、持续回复等）每回合自动生效。

        这是引擎级硬保证，不依赖裁判每回合记得重判。
        """
        ticks: list[dict] = []
        deaths: list[str] = []
        for p in alive:
            for attr in list(p.attributes.values()):
                if attr.per_turn == 0:
                    continue
                before = p.hp
                p.hp = max(0, min(p.hp_max, before + attr.per_turn))
                actual = p.hp - before
                if actual == 0:
                    continue
                dead = False
                if p.hp == 0 and attr.per_turn < 0:
                    p.alive = False
                    dead = True
                    deaths.append(p.player_id)
                ticks.append(
                    {
                        "player_id": p.player_id,
                        "attr_id": attr.attr_id,
                        "name": attr.name,
                        "delta": actual,
                        "hp": p.hp,
                        "dead": dead,
                    }
                )
        if not ticks:
            return alive
        self._record(
            LedgerType.SYSTEM,
            "，".join(
                f"{self.players[t['player_id']].name} 的「{t['name']}」"
                f"{'发作' if t['delta'] < 0 else '生效'}"
                f"{t['delta']:+d} → {t['hp']}/{self.players[t['player_id']].hp_max}"
                + ("，当场出局" if t["dead"] else "")
                for t in ticks
            )
            + "。",
            payload={"kind": "attribute_tick", "changes": ticks},
        )
        for t in ticks:
            await self._broadcast(
                {
                    "type": "hp_update",
                    "player_id": t["player_id"],
                    "hp": t["hp"],
                    "hp_max": self.players[t["player_id"]].hp_max,
                    "delta": t["delta"],
                }
            )
        for pid in deaths:
            victim = self.players[pid]
            self._record(
                LedgerType.DEATH,
                f"{victim.name} 出局。",
                actor=pid,
                payload={"player_id": pid, "killer": pid, "cause": "attr_tick"},
            )
            await self._broadcast(
                {
                    "type": "player_dead",
                    "player_id": pid,
                    "name": victim.name,
                    "cause": "attr_tick",
                    "killer": pid,
                }
            )
        await self.broadcast_state()
        return [p for p in alive if p.alive]

    async def _play_turn(self, alive: list[Player]) -> None:
        self.turn_no += 1
        for p in alive:
            p.survived_turns += 1
            if self.turn_no > 1:
                p.hp = min(p.hp_max, p.hp + self.pack.hp.regen)
            p.hp_peak = max(p.hp_peak, p.hp)

        alive = await self._run_per_turn(alive)
        if not alive:
            await self._finish([])
            return

        actor = self._next_actor(alive)
        self.current_actor = actor.player_id
        self.phase = Phase.DECLARE
        self._collector.reset()
        regen_note = (
            f"，所有存活玩家至多回复 {self.pack.hp.regen} 点血量"
            if self.turn_no > 1 and self.pack.hp.regen > 0
            else ""
        )
        self._record(
            LedgerType.SYSTEM,
            f"第 {self.turn_no} 回合，轮到 {actor.name}{regen_note}。",
            payload={
                "kind": "turn_begin",
                "turn_no": self.turn_no,
                "current_player": actor.player_id,
                "hp": {p.player_id: p.hp for p in self.players.values() if p.alive},
            },
        )
        await self._expire_attributes(alive)

        # 冻结类能力：引擎硬保证，被冻住直接跳过这一回合（无法宣言）。
        # 按目标自己被跳过的回合数计费，扣完次数才解冻。
        frozen = next((a for a in actor.attributes.values() if a.freeze), None)
        if frozen is not None:
            frozen.charges = max(0, frozen.charges - 1)
            expired = frozen.charges == 0
            left = frozen.charges
            debug_log.log("frozen_skip", turn=self.turn_no, player=actor.name,
                          attr=frozen.name, charges_left=left,
                          expired=expired)  # [TEMP-LOG]
            self._record(
                LedgerType.SYSTEM,
                f"{actor.name} 被「{frozen.name}」困住，这一回合完全无法行动。"
                + ("" if expired else f"（还剩 {left} 回合）"),
                payload={
                    "kind": "frozen_skip",
                    "player_id": actor.player_id,
                    "attr_id": frozen.attr_id,
                    "charges_left": left,
                    "expired": expired,
                },
            )
            await self._broadcast(
                {
                    "type": "frozen_skip",
                    "turn_no": self.turn_no,
                    "player_id": actor.player_id,
                    "name": actor.name,
                    "attr": frozen.name,
                    "charges_left": left,
                }
            )
            if expired:
                actor.attributes.pop(frozen.attr_id, None)
                self._record(
                    LedgerType.SYSTEM,
                    f"{actor.name} 的「{frozen.name}」消散了，寒气散尽。",
                    payload={
                        "kind": "attribute_expired",
                        "player_id": actor.player_id,
                        "attr_id": frozen.attr_id,
                    },
                )
                await self._broadcast(
                    {
                        "type": "attribute_removed",
                        "player_id": actor.player_id,
                        "player_name": actor.name,
                        "attr_id": frozen.attr_id,
                        "name": frozen.name,
                        "reason": "expired",
                    }
                )
            await self.broadcast_state()
            return

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

        result = await judge_ai.adjudicate(
            players=self.players,
            turn_no=self.turn_no,
            pack=self.pack,
            decl=decl,
            ledger=self.ledger,
            world_summary=self.world_summary,
            base_url=self.judge_base_url,
            model=self.judge_model,
            budget=max(5.0, float(self.pack.timers.judge_s) - 10.0),
        )
        verdict = result.verdict
        if result.clamped:
            verdict.narrative = f"{verdict.narrative}（数值经服务端钳制）"

        if result.source != "ai":
            entry = self._record(
                LedgerType.SYSTEM,
                f"裁判降级：{result.reason}",
                payload={"kind": "judge_fallback", "reason": result.reason},
            )
            await self._broadcast(
                {
                    "type": "judge_fallback",
                    "reason": result.reason,
                    "ledger_seq": entry.seq,
                    "turn_no": self.turn_no,
                }
            )

        # 属性先落地（新建 / 变化），只给本人回执
        attr_updates: list[dict] = []
        for change in verdict.attr_changes:
            owner = self.players.get(change.get("owner"))
            if owner is None:
                debug_log.log("attr_apply_miss", turn=self.turn_no,
                              reason="owner-not-found", change=change)  # [TEMP-LOG]
                continue
            if change.get("remove"):
                attr_id = str(change.get("attr_id"))
                attr = owner.attributes.pop(attr_id, None)
                if attr is None:
                    debug_log.log("attr_apply_miss", turn=self.turn_no,
                                  reason="remove-pop-miss", owner=owner.name, change=change)  # [TEMP-LOG]
                    continue
                debug_log.log("attr_apply", turn=self.turn_no, action="removed",
                              owner=owner.name, attr=attr.name)  # [TEMP-LOG]
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
                    debug_log.log("attr_apply_miss", turn=self.turn_no,
                                  reason="max-count", owner=owner.name, change=change)  # [TEMP-LOG]
                    continue
                if any(a.name == change.get("name") for a in owner.attributes.values()):
                    debug_log.log("attr_apply_miss", turn=self.turn_no,
                                  reason="dup-name", owner=owner.name, change=change)  # [TEMP-LOG]
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
                    duration=int(change.get("duration", 0)),
                    shield=bool(change.get("shield", False)),
                    freeze=bool(change.get("freeze", False)),
                    per_turn=int(change.get("per_turn", 0)),
                )
                owner.attributes[attr.attr_id] = attr
                debug_log.log("attr_apply", turn=self.turn_no, action="created",
                              owner=owner.name, attr=attr.name, shield=attr.shield,
                              freeze=attr.freeze, per_turn=attr.per_turn,
                              expires_turn=attr.expires_turn)  # [TEMP-LOG]
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
                        "value": attr.value,
                        "cap": attr.cap,
                        "expires_turn": attr.expires_turn,
                    }
                )
                await self._send(
                    owner.player_id,
                    {"type": "attribute_private", "notice": "新属性", "attr": attr.private_view()},
                )
                continue

            attr = owner.attributes.get(str(change.get("attr_id")))
            if attr is None:
                debug_log.log("attr_apply_miss", turn=self.turn_no,
                              reason="value-attr-miss", owner=owner.name, change=change)  # [TEMP-LOG]
                continue
            attr.value = max(0, min(attr.cap, int(change.get("value", attr.value))))
            debug_log.log("attr_apply", turn=self.turn_no, action="value-set",
                          owner=owner.name, attr=attr.name, value=attr.value)  # [TEMP-LOG]
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

        # 伤害 / 治疗：防御属性在这里统一结算（抵扣 + 磨损）
        applied: list[dict] = []
        deaths: list[tuple[str, str]] = []
        for effect in verdict.effects:
            pid = str(effect.get("player_id"))
            target = self.players.get(pid)
            if target is None or not target.alive:
                continue
            delta = int(effect.get("hp_delta", 0))
            absorbed = 0
            if delta < 0 and not effect.get("pierce"):
                # pierce：宣言明确声明无视免伤特效，本次穿透（属性保留）
                shield_attr = next(
                    (a for a in target.attributes.values() if a.shield), None
                )
                if shield_attr is not None:
                    # 能力优先级最高：免伤类属性存在期间，宣言没先破坏它，
                    # 伤害一律无效（引擎硬保证，不依赖裁判自觉）
                    applied.append(
                        {
                            "player_id": pid,
                            "hp_delta": 0,
                            "hp": target.hp,
                            "absorbed": 0,
                            "shield": shield_attr.name,
                        }
                    )
                    continue
                defense, used = effective_defense(target.attributes.values())
                if defense > 0 and used:
                    resolved = resolve_damage(-delta, defense)
                    absorbed = -delta - resolved
                    delta = -resolved
                    if absorbed > 0:
                        for wear in apply_armor_wear(used, absorbed):
                            attr_updates.append(
                                {
                                    "owner": target.player_id,
                                    "attr_id": wear["attr_id"],
                                    "name": wear["name"],
                                    "value": wear["value"],
                                }
                            )
                            attr = target.attributes.get(wear["attr_id"])
                            if attr is not None:
                                await self._send(
                                    target.player_id,
                                    {
                                        "type": "attribute_private",
                                        "notice": "变化",
                                        "attr": attr.private_view(),
                                    },
                                )
            before = target.hp
            target.hp = max(0, min(target.hp_max, before + delta))
            actual = target.hp - before
            if actual == 0:
                continue
            applied.append(
                {
                    "player_id": pid,
                    "hp_delta": actual,
                    "hp": target.hp,
                    "absorbed": absorbed,
                }
            )
            if target.hp == 0 and actual < 0:
                target.alive = False
                deaths.append((pid, actor.player_id))

        # 用服务端实际结算数值补一条括号，避免模型叙事里的数字与实际血量对不上
        settle_bits = []
        for item in applied:
            target = self.players[item["player_id"]]
            if item.get("shield"):
                settle_bits.append(
                    f"{target.name} 的「{item['shield']}」生效，伤害被完全挡下"
                )
            elif item.get("absorbed"):
                settle_bits.append(
                    f"{target.name} 护甲挡下 {item['absorbed']}，实际 "
                    f"{item['hp_delta']:+d} → {item['hp']}/{target.hp_max}"
                )
            else:
                settle_bits.append(
                    f"{target.name} {item['hp_delta']:+d} → {item['hp']}/{target.hp_max}"
                )
        settle_bits.extend(f"{self.players[pid].name} 出局" for pid, _ in deaths)
        debug_log.log("settle", turn=self.turn_no, actor=actor.name,
                      applied=applied, deaths=[self.players[pid].name for pid, _ in deaths],
                      decl=decl.content[:120])  # [TEMP-LOG]
        if settle_bits:
            verdict.narrative = f"{verdict.narrative}（结算：{'；'.join(settle_bits)}）"

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
                "source": result.source,
                "clamped": result.clamped,
                "attempts": result.attempts,
            },
        )

        await self._broadcast(
            {
                "type": "adjudication",
                "ledger_seq": entry.seq,
                "narrative": verdict.narrative,
                "source": result.source,
                "clamped": result.clamped,
                "effects": [
                    {
                        "player_id": item["player_id"],
                        "delta": item["hp_delta"],
                        "hp": item["hp"],
                    }
                    for item in applied
                    if item["hp_delta"] != 0
                ],
            }
        )

        for item in applied:
            if item["hp_delta"] == 0:
                continue
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
        self._schedule_summary()

    def _schedule_summary(self) -> None:
        if self.turn_no < 10 or self.turn_no % 10 != 0:
            return
        if self.summary_turn >= self.turn_no:
            return
        self.summary_turn = self.turn_no
        self._summary_task = asyncio.create_task(self._refresh_summary())

    async def _refresh_summary(self) -> None:
        text = await summary.compress(
            previous=self.world_summary,
            players=self.players,
            entries=self.ledger[-60:],
            base_url=self.judge_base_url,
            model=self.judge_model,
        )
        if not text or self._closed:
            return
        self.world_summary = text
        self._record(
            LedgerType.SYSTEM,
            "世界史更新。",
            payload={"kind": "world_summary", "text": text, "turn_no": self.turn_no},
        )
        await self._broadcast(
            {"type": "world_summary", "text": text, "turn_no": self.turn_no}
        )

    async def _finish(self, alive: list[Player]) -> None:
        self.over = True
        self.phase = Phase.OVER
        self.deadline = None
        winner = alive[0] if len(alive) == 1 else None
        self.winner = winner.player_id if winner else None
        result = self.result_view() or {"winner_name": None, "ranking": []}
        self._record(
            LedgerType.SYSTEM,
            f"{winner.name} 撑到了最后。" if winner else "平局，所有玩家全部死亡。",
            payload={
                "kind": "game_over",
                "winner": self.winner,
                "ranking": result["ranking"],
            },
        )
        await self._broadcast(
            {
                "type": "game_over",
                "winner": self.winner,
                "winner_name": result["winner_name"],
                "ranking": result["ranking"],
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
        decl = brain.declare(bot, self.players, self.turn_no, self.pack)
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
                "value": attr.value,
                "cap": attr.cap,
                "expires_turn": attr.expires_turn,
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
