"""对局引擎：回合状态机 + 事件记账 + 广播。

设计要点：
- 四阶段 DECLARE → ADJUDICATE → SETTLE → NEXT，时限全部由服务端驱动。
- 玩家的输入只有一段自由文本；效果与目标由裁判判断。
- 受到伤害时没有即时回应，直接按当前文本与属性结算。
- 一切状态变更都经过 _record（写事件）与内存投影，保证广播与界面一致。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from typing import Optional

from . import attributes as attr_mod
from . import debug_log  # [TEMP-LOG] 临时调试日志，稳定后与调用点一起删除
from . import disaster, events, inheritance, judge_ai, summary
from .bots import BotBrain
from .effect_runtime import EffectRuntime
from .formulas import IntensityRules
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
        self._over_since: Optional[float] = None
        self._idle_since: Optional[float] = None
        self.end_reason = ""
        self.winner: Optional[str] = None
        self.current_actor: Optional[str] = None
        self.deadline: Optional[float] = None
        self.bot_brains: dict[str, BotBrain] = {}
        self.event_memory: dict[str, events.EventMemory] = {}
        self.effects = EffectRuntime(self.players, self.pack)
        self.rng = random.Random()

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
        self.reassign_host()
        self._record(
            LedgerType.SYSTEM,
            f"{name} 离开了房间。",
            payload={"kind": "player_left", "player_id": pid},
        )

    def reassign_host(self) -> None:
        current = self.players.get(self.host_id)
        if current and current.kind == "human" and current.connected:
            return
        humans = [p for p in self.players.values() if p.kind == "human"]
        candidate = next((p for p in humans if p.connected), None)
        successor = candidate or (humans[0] if humans else None)
        self.host_id = successor.player_id if successor else ""

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
        player = self.players.get(pid)
        if player is None or not player.connected:
            return
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
            key=lambda p: (p.alive, p.hp, *(getattr(p, key) for key in self.pack.tie_break)),
            reverse=True,
        )

    def result_view(self) -> Optional[dict]:
        """结算视图：刷新或断线重连后仍能还原胜者与排名。"""
        if not self.over:
            return None
        winner = self.players.get(self.winner) if self.winner else None
        return {
            "reason": self.end_reason,
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
            "disaster": self.disaster_view(),
            "active_effects": self.effects.view(),
        }

    def disaster_view(self) -> dict:
        level = disaster.level_for(self.pack, self.turn_no)
        return {"level": level, "damage": disaster.damage_for(self.pack, level)}

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
        if self._summary_task and not self._summary_task.done():
            self._summary_task.cancel()
            try:
                await self._summary_task
            except (asyncio.CancelledError, Exception):
                pass
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
        self._over_since = None
        self._idle_since = None
        self.end_reason = ""
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
        self.event_memory.clear()
        self.effects.reset()
        self.effects.pack = self.pack
        for pid, player in list(self.players.items()):
            if player.kind == "human" and not player.connected:
                self.remove_player(pid)
        for player in self.players.values():
            player.hp = self.pack.hp.base
            player.hp_max = self.pack.hp.base
            player.hp_peak = self.pack.hp.base
            player.alive = True
            player.survived_turns = 0
            player.attributes.clear()
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
            await self._finish([], reason="对局发生异常，已终止；房主可以返回大厅重新开局。")

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

    async def _run_disaster(self, alive: list[Player]) -> list[Player]:
        """天灾：环境伤害，无视护甲与免伤，用于强制收束战局。"""
        level = disaster.level_for(self.pack, self.turn_no)
        damage = disaster.damage_for(self.pack, level)
        if level <= 0 or damage <= 0:
            return alive

        self.effects.begin(self.turn_no)
        changes: list[dict] = []
        deaths: list[str] = []
        for p in alive:
            before = p.hp
            p.hp = max(0, before - damage)
            dead = p.hp == 0
            if dead:
                p.alive = False
                deaths.append(p.player_id)
            self.effects.expire_conditions()
            changes.append(
                {
                    "player_id": p.player_id,
                    "delta": p.hp - before,
                    "hp": p.hp,
                    "dead": dead,
                }
            )
        if not changes:
            return alive

        # 账本逐回合记录，保证回看时血条变化有据可查；提示只在等级提升时推送
        self._record(
            LedgerType.DISASTER,
            disaster.narrative(self.pack.disaster, self.turn_no, level, damage),
            payload={
                "kind": "disaster",
                "level": level,
                "damage": damage,
                "changes": changes,
            },
        )
        if disaster.is_new_level(self.pack.disaster, self.turn_no):
            await self._broadcast(
                {
                    "type": "disaster",
                    "turn_no": self.turn_no,
                    "level": level,
                    "damage": damage,
                    "changes": changes,
                }
            )
        for c in changes:
            await self._broadcast(
                {
                    "type": "hp_update",
                    "player_id": c["player_id"],
                    "hp": c["hp"],
                    "hp_max": self.players[c["player_id"]].hp_max,
                    "delta": c["delta"],
                }
            )
        for pid in deaths:
            victim = self.players[pid]
            self._record(
                LedgerType.DEATH,
                f"{victim.name} 在天灾中出局。",
                actor=pid,
                payload={"player_id": pid, "killer": None, "cause": "disaster"},
            )
            await self._broadcast(
                {
                    "type": "player_dead",
                    "player_id": pid,
                    "name": victim.name,
                    "cause": "disaster",
                    "killer": None,
                }
            )
        triggered = self.effects.observe_environment(self.turn_no, changes, continuing=True)
        if triggered:
            await self._publish_effect_records(triggered)
        await self.broadcast_state()
        return [p for p in alive if p.alive]

    async def _run_events(self, alive: list[Player]) -> list[Player]:
        """特殊事件：按扩展包定义逐条判定，结算伤害/回血并授予属性。

        与天灾一样由服务端硬结算、不经裁判；事件致死记 killer=None，不触发继承。
        """
        if not self.pack.events:
            return alive

        fired = False
        deaths: list[str] = []
        for cfg in self.pack.events:
            memory = self.event_memory.setdefault(cfg.id, events.EventMemory())
            if not events.should_fire(cfg, self.turn_no, memory, self.rng):
                continue
            current = [p for p in alive if p.alive]
            if (deaths or fired) and len(current) <= 1:
                break
            targets = events.pick_targets(cfg, current, self.rng)
            if not targets:
                continue
            fired = True
            memory.times += 1
            memory.last_turn = self.turn_no

            if cfg.mode == "judged":
                result = await judge_ai.adjudicate_event(
                    players=self.players,
                    turn_no=self.turn_no,
                    pack=self.pack,
                    event_name=cfg.name,
                    event_prompt=cfg.judge_prompt,
                    targets=targets,
                    ledger=self.ledger,
                    world_summary=self.world_summary,
                    base_url=self.judge_base_url,
                    model=self.judge_model,
                    budget=max(5.0, float(self.pack.timers.judge_s) - 10.0),
                    effect_context=self.effects.context(),
                )
                if result.source == "ai" and result.verdict.narrative:
                    narrative, effects, dead = await self._apply_judged_event(result)
                    deaths.extend(dead)
                    self._record(
                        LedgerType.EVENT,
                        narrative,
                        payload={
                            "kind": "special_event",
                            "event_id": cfg.id,
                            "name": cfg.name,
                            "effects": effects,
                            "source": "ai",
                        },
                    )
                    await self._broadcast(
                        {
                            "type": "special_event",
                            "turn_no": self.turn_no,
                            "event_id": cfg.id,
                            "name": cfg.name,
                            "narrative": narrative,
                            "effects": effects,
                            "source": "ai",
                        }
                    )
                    for e in effects:
                        if e["delta"]:
                            await self._broadcast(
                                {
                                    "type": "hp_update",
                                    "player_id": e["player_id"],
                                    "hp": e["hp"],
                                    "hp_max": self.players[e["player_id"]].hp_max,
                                    "delta": e["delta"],
                                }
                            )
                    triggered = self.effects.observe_environment(self.turn_no, effects, continuing=True)
                    if triggered:
                        await self._publish_effect_records(triggered)
                    continue
                # AI 不可用：退回事件自带的固定效果

            self.effects.begin(self.turn_no)
            effects: list[dict] = []
            for p in targets:
                before = p.hp
                delta = events.hp_delta(cfg, p)
                if delta:
                    p.hp = max(0, min(p.hp_max, before + delta))
                dead = p.hp == 0
                if dead:
                    p.alive = False
                    deaths.append(p.player_id)
                self.effects.expire_conditions()
                granted = ""
                if cfg.attribute is not None and events.can_grant(
                    self.pack, cfg.attribute, p
                ) and (len(p.attributes) + sum(p.player_id in e.spec.targets for e in self.effects.active.values())
                       < self.pack.attributes.max_count):
                    attr = events.build_attribute(
                        cfg.attribute, p, self.turn_no, self.pack.attributes.max_cap
                    )
                    p.attributes[attr.attr_id] = attr
                    granted = attr.name
                effects.append(
                    {
                        "player_id": p.player_id,
                        "name": p.name,
                        "delta": p.hp - before,
                        "hp": p.hp,
                        "dead": dead,
                        "attribute": granted,
                    }
                )

            narrative = events.render(cfg, targets)
            self._record(
                LedgerType.EVENT,
                narrative,
                payload={
                    "kind": "special_event",
                    "event_id": cfg.id,
                    "name": cfg.name,
                    "effects": effects,
                },
            )
            await self._broadcast(
                {
                    "type": "special_event",
                    "turn_no": self.turn_no,
                    "event_id": cfg.id,
                    "name": cfg.name,
                    "narrative": narrative,
                    "effects": effects,
                }
            )
            for e in effects:
                if e["delta"]:
                    await self._broadcast(
                        {
                            "type": "hp_update",
                            "player_id": e["player_id"],
                            "hp": e["hp"],
                            "hp_max": self.players[e["player_id"]].hp_max,
                            "delta": e["delta"],
                        }
                    )
                if e["attribute"]:
                    attr_cfg = cfg.attribute
                    await self._broadcast(
                        {
                            "type": "attribute_opened",
                            "player_id": e["player_id"],
                            "player_name": e["name"],
                            "name": e["attribute"],
                            "narrative": attr_cfg.narrative if attr_cfg else "",
                            "attr_type": attr_cfg.attr_type if attr_cfg else "utility",
                        }
                    )

            triggered = self.effects.observe_environment(self.turn_no, effects, continuing=True)
            if triggered:
                await self._publish_effect_records(triggered)

        if not fired:
            return alive

        for pid in deaths:
            victim = self.players[pid]
            self._record(
                LedgerType.DEATH,
                f"{victim.name} 在特殊事件中出局。",
                actor=pid,
                payload={"player_id": pid, "killer": None, "cause": "event"},
            )
            await self._broadcast(
                {
                    "type": "player_dead",
                    "player_id": pid,
                    "name": victim.name,
                    "cause": "event",
                    "killer": None,
                }
            )
        await self.broadcast_state()
        return [p for p in alive if p.alive]

    async def _apply_judged_event(
        self, result: judge_ai.JudgeResult
    ) -> tuple[str, list[dict], list[str]]:
        """落地 judged 事件：环境伤害直接生效，不受护甲/免伤二次结算。"""
        verdict = result.verdict
        narrative = verdict.narrative
        if result.clamped:
            narrative = f"{narrative}（数值经服务端钳制）"
        await self._apply_attr_changes(verdict.attr_changes, None)
        self.effects.begin(self.turn_no)
        effects: list[dict] = []
        deaths: list[str] = []
        for eff in verdict.effects:
            target = self.players.get(str(eff.get("player_id")))
            if target is None or not target.alive:
                continue
            before = target.hp
            target.hp = max(0, min(target.hp_max, before + int(eff.get("hp_delta", 0))))
            actual = target.hp - before
            if actual == 0:
                continue
            dead = target.hp == 0
            if dead:
                target.alive = False
                deaths.append(target.player_id)
            self.effects.expire_conditions()
            effects.append(
                {
                    "player_id": target.player_id,
                    "name": target.name,
                    "delta": actual,
                    "hp": target.hp,
                    "dead": dead,
                    "attribute": "",
                }
            )
        bits = [
            f"{e['name']} {e['delta']:+d} → {e['hp']}/{self.players[e['player_id']].hp_max}"
            for e in effects
        ]
        bits.extend(f"{self.players[pid].name} 出局" for pid in deaths)
        if bits:
            narrative = f"{narrative}（结算：{'；'.join(bits)}）"
        return narrative, effects, deaths

    async def _run_per_turn(self, alive: list[Player]) -> list[Player]:
        """周期效果结算：带 per_turn 的属性（中毒、灼烧、持续回复等）每回合自动生效。

        这是引擎级硬保证，不依赖裁判每回合记得重判。
        """
        self.effects.begin(self.turn_no)
        ticks: list[dict] = []
        deaths: list[str] = []
        for p in alive:
            for attr in list(p.attributes.values()):
                if not p.alive:
                    break
                if attr.expires_turn and self.turn_no > attr.expires_turn:
                    continue
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
                self.effects.expire_conditions()
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
            if self.effects.records:
                await self._publish_effect_records(list(self.effects.records))
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
        triggered = self.effects.observe_environment(self.turn_no, ticks, continuing=True)
        if triggered:
            await self._publish_effect_records(triggered)
        await self.broadcast_state()
        return [p for p in alive if p.alive]

    async def _play_turn(self, alive: list[Player]) -> None:
        try:
            await self._play_turn_body(alive)
        finally:
            if self.current_actor:
                records = self.effects.end_turn(self.turn_no, self.current_actor)
                if records:
                    await self._publish_effect_records(records)
                else:
                    await self.broadcast_state()

    async def _play_turn_body(self, alive: list[Player]) -> None:
        self.current_actor = None
        self.turn_no += 1
        await self._expire_attributes(alive)
        for p in alive:
            p.survived_turns += 1
            if self.turn_no > 1:
                p.hp = min(p.hp_max, p.hp + self.pack.hp.regen)
            p.hp_peak = max(p.hp_peak, p.hp)

        alive = await self._run_disaster(alive)
        if len(alive) <= 1:
            await self._finish(alive)
            return

        alive = await self._run_events(alive)
        if len(alive) <= 1:
            await self._finish(alive)
            return

        alive = await self._run_per_turn(alive)
        if len(alive) <= 1:
            await self._finish(alive)
            return

        actor = self._next_actor(alive)
        self.current_actor = actor.player_id
        records = self.effects.start_turn(self.turn_no, actor.player_id)
        if records:
            await self._publish_effect_records(records)
        alive = self.alive_players()
        if len(alive) <= 1:
            await self._finish(alive)
            return
        if not actor.alive:
            return
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
        # 冻结类能力：引擎硬保证，被冻住直接跳过这一回合（无法宣言）。
        # 按目标自己被跳过的回合数计费，扣完次数才解冻。
        if self.effects.frozen(actor.player_id):
            self._record(LedgerType.SYSTEM, f"{actor.name} 受跳过行动效果约束，本次无法行动。",
                         payload={"kind": "effect_skip", "player_id": actor.player_id})
            await self.broadcast_state()
            return
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

    def _guard_verdict(self, verdict, actor: Player) -> list[str]:
        """结算前强制校验现存规则，模板降级和 AI 输出走同一条路径。"""
        if any(attr.freeze for attr in actor.attributes.values()):
            verdict.effects = []
            verdict.attr_changes = []
            return [f"{actor.name} 仍被禁锢，本次行动无效。"]
        notes = []
        for change in verdict.attr_changes:
            owner = self.players.get(change.get("owner"))
            attr = owner.attributes.get(str(change.get("attr_id"))) if owner else None
            if attr and attr.indestructible and (attr.freeze or actor.player_id != owner.player_id):
                notes.append(f"{owner.name} 的「{attr.name}」仍然生效，改写或转移被阻止。")
        for effect in verdict.effects:
            target = self.players.get(effect.get("player_id"))
            if (target and effect.get("pierce") and int(effect.get("hp_delta", 0)) < 0
                    and any(a.shield and a.indestructible for a in target.attributes.values())):
                effect.pop("pierce", None)
                notes.append(f"{target.name} 的不可破坏防护仍然生效，不能凭宣言绕过。")
        return notes

    async def _apply_attr_changes(
        self, changes: list[dict], actor: Optional[Player]
    ) -> list[dict]:
        """落地属性变更（新建 / 改值 / 删除），返回实际发生的变更。"""
        actor_id = actor.player_id if actor else ""
        actor_name = actor.name if actor else "环境"
        hard = IntensityRules(self.pack.intensity.cap_base, self.pack.intensity.cap_growth,
                              self.pack.intensity.ceiling_factor).hard(self.turn_no)
        # 属性先落地（新建 / 变化），只给本人回执
        attr_updates: list[dict] = []
        # 预先扫描，防止把“偷取后的新属性”排在 remove 前绕过保护。
        blocked_names: set[str] = set()
        for change in changes:
            owner = self.players.get(change.get("owner"))
            attr = owner.attributes.get(str(change.get("attr_id"))) if owner else None
            if (attr and change.get("remove") and attr.indestructible
                    and (attr.freeze or actor_id != owner.player_id)):
                blocked_names.add(attr.name)
        for change in changes:
            owner = self.players.get(change.get("owner"))
            if owner is None:
                debug_log.log("attr_apply_miss", turn=self.turn_no,
                              reason="owner-not-found", change=change)  # [TEMP-LOG]
                continue
            if change.get("remove"):
                attr_id = str(change.get("attr_id"))
                attr = owner.attributes.get(attr_id)
                if (attr is not None and attr.indestructible
                        and (attr.freeze or actor_id != owner.player_id)):
                    debug_log.log("attr_apply_blocked", turn=self.turn_no,
                                  reason="indestructible", owner=owner.name,
                                  attr=attr.name, actor=actor_name)  # [TEMP-LOG]
                    self._record(
                        LedgerType.SYSTEM,
                        f"{owner.name} 的「{attr.name}」不可破坏，没有被摧毁。",
                        payload={
                            "kind": "attr_indestructible",
                            "owner": owner.player_id,
                            "attr_id": attr_id,
                            "actor": actor_id,
                        },
                    )
                    blocked_names.add(attr.name)
                    continue
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
                if (len(owner.attributes) + sum(owner.player_id in e.spec.targets
                        for e in self.effects.active.values()) >= self.pack.attributes.max_count):
                    debug_log.log("attr_apply_miss", turn=self.turn_no,
                                  reason="max-count", owner=owner.name, change=change)  # [TEMP-LOG]
                    continue
                if any(a.name == change.get("name") for a in owner.attributes.values()):
                    debug_log.log("attr_apply_miss", turn=self.turn_no,
                                  reason="dup-name", owner=owner.name, change=change)  # [TEMP-LOG]
                    continue
                # 不可破坏的属性既然没被移除，同一次裁定里「偷到手」的那份也不成立
                if change.get("name") in blocked_names:
                    debug_log.log("attr_apply_blocked", turn=self.turn_no,
                                  reason="indestructible-steal", owner=owner.name,
                                  attr=change.get("name"))  # [TEMP-LOG]
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
                    indestructible=bool(change.get("indestructible", False)),
                    damage_reduction=int(change.get("damage_reduction", 0)),
                    shield_charges=int(change.get("shield_charges", 0)),
                    lethal_only=bool(change.get("lethal_only", False)),
                    block_heal_pct=int(change.get("block_heal_pct", 0)),
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
                        "indestructible": attr.indestructible,
                        "damage_reduction": attr.damage_reduction,
                        "shield_charges": attr.shield_charges,
                        "lethal_only": attr.lethal_only,
                        "block_heal_pct": attr.block_heal_pct,
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
            if attr.indestructible and (attr.freeze or actor_id != owner.player_id):
                continue
            old_freeze = attr.freeze
            if "cap" in change:
                attr.cap = max(1, min(self.pack.attributes.max_cap, int(change["cap"])))
            attr.value = max(0, min(attr.cap, int(change.get("value", attr.value))))
            for key in ("shield", "freeze", "indestructible", "lethal_only"):
                if key in change:
                    setattr(attr, key, bool(change[key]))
            for key, low, high in (
                ("per_turn", -hard, hard),
                ("damage_reduction", 0, 90), ("shield_charges", 0, 9),
                ("block_heal_pct", 0, 100),
            ):
                if key in change:
                    setattr(attr, key, max(low, min(high, int(change[key]))))
            if "duration" in change:
                duration = max(0, min(9, int(change["duration"])))
                attr.expires_turn = 0 if attr.freeze or not duration else self.turn_no + duration - 1
                attr.charges = max(1, duration) if attr.freeze else 0
            elif attr.freeze != old_freeze:
                attr.charges = 1 if attr.freeze else 0
                attr.expires_turn = 0
            if attr.shield_charges > 0 or attr.block_heal_pct > 0:
                attr.shield = True
            if change.get("attr_type") in attr_mod.ATTR_TYPES:
                attr.attr_type = change["attr_type"]
            if "narrative" in change:
                attr.narrative = str(change["narrative"])[:200]
            debug_log.log("attr_apply", turn=self.turn_no, action="value-set",
                          owner=owner.name, attr=attr.name, value=attr.value)  # [TEMP-LOG]
            attr_updates.append(
                {
                    "owner": owner.player_id,
                    "attr_id": attr.attr_id,
                    "name": attr.name,
                    **attr.private_view(),
                }
            )
            await self._send(
                owner.player_id,
                {"type": "attribute_private", "notice": "变化", "attr": attr.private_view()},
            )
        return attr_updates


    async def _publish_effect_records(self, records, actor=None, result=None, decl=None):
        """同一执行记录驱动战报、血量消息和能力视图；不展示模型成功叙事。"""
        bits = []
        hp_records = [record for record in records if record["kind"] == "hp"]
        for record in records:
            if record["kind"] == "hp":
                target = self.players[record["player_id"]]
                bits.append(f"{target.name} 因「{record['name']}」{record['hp_delta']:+d} → {record['hp']}/{target.hp_max}"
                            + ("，出局" if record.get("dead") else ""))
            elif record.get("text"):
                bits.append(record["text"])
        narrative = "；".join(bits) or "本次没有产生状态变化。"
        payload = {"kind": "effect_execution", "records": records,
                   "source": result.source if result else "engine",
                   "attempts": result.attempts if result else 0,
                   "reason": result.reason if result else "",
                   "plan": result.plan.model_dump() if result and result.plan else None}
        if actor:
            self.phase = Phase.SETTLE
            self.deadline = None
            await self._broadcast({"type": "phase_change", "phase": Phase.SETTLE.value, "deadline": None})
            entry = self._record(LedgerType.ADJUDICATION, narrative, actor=actor.player_id, payload=payload)
            await self._broadcast({"type": "adjudication", "ledger_seq": entry.seq,
                "narrative": narrative, "source": result.source, "clamped": any(r["kind"] == "limited" for r in records),
                "effects": [{"player_id": r["player_id"], "delta": r["hp_delta"], "hp": r["hp"]} for r in hp_records]})
        elif records:
            entry = self._record(LedgerType.SYSTEM, narrative, payload=payload)
            await self._broadcast({"type": "effect_event", "ledger_seq": entry.seq,
                                   "turn_no": self.turn_no, "narrative": narrative})
        for record in hp_records:
            target = self.players[record["player_id"]]
            await self._broadcast({"type": "hp_update", "player_id": target.player_id,
                "hp": record["hp"], "hp_max": target.hp_max, "delta": record["hp_delta"]})
            if record.get("dead"):
                killer = record.get("source")
                self._record(LedgerType.DEATH, f"{target.name} 出局。", actor=target.player_id,
                    payload={"player_id": target.player_id, "killer": killer, "cause": "effect"})
                await self._broadcast({"type": "player_dead", "player_id": target.player_id,
                    "name": target.name, "killer": killer, "cause": "effect"})
                await self._grant_inheritance(target, killer)
        debug_log.log("effect_execution", turn=self.turn_no,
            actor=actor.player_id if actor else None, declaration=decl.content if decl else None,
            records=records, active_effects=self.effects.view())
        await self.broadcast_state()
        if actor:
            self._schedule_summary()

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
            active_effects=self.effects.view(),
        )
        if result.plan is not None:
            records = self.effects.apply(result.plan, actor.player_id, self.turn_no)
            if result.source == "stub" and result.reason:
                records.insert(0, {"kind": "fallback", "text": f"裁判降级：{result.reason}。"})
            await self._publish_effect_records(records, actor=actor, result=result, decl=decl)
            return
        if result.source == "rejected":
            await self._publish_effect_records([{"kind": "unsupported", "text": result.reason}],
                                              actor=actor, result=result, decl=decl)
            return
        verdict = result.verdict
        rule_notes = self._guard_verdict(verdict, actor)
        if rule_notes:
            # 不展示被拦截效果的成功叙事，实际伤害仍由下方结算摘要补齐。
            verdict.narrative = " ".join(rule_notes)
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

        attr_updates = await self._apply_attr_changes(verdict.attr_changes, actor)

        # 旧扩展包/模板输出也进入同一个伤害解释器，不能绕过新能力。
        self.effects.begin(self.turn_no)
        applied: list[dict] = []
        deaths: list[tuple[str, str]] = []
        for effect in verdict.effects:
            pid = str(effect.get("player_id"))
            target = self.players.get(pid)
            if target is None or not target.alive:
                continue
            delta = int(effect.get("hp_delta", 0))
            if delta < 0:
                self.effects.damage(pid, -delta, actor.player_id, "行动", pierce=bool(effect.get("pierce")))
            else:
                self.effects.heal(pid, delta, actor.player_id, "行动")
            self.effects.drain_queue()
        for record in self.effects.records:
            if record["kind"] == "hp" or record.get("shield"):
                applied.append(record)
            if record.get("dead"):
                deaths.append((record["player_id"], record["source"]))
        notes = [r["text"] for r in self.effects.records if r.get("text")]
        if notes:
            verdict.narrative = "；".join(notes)

        # 用服务端实际结算数值补一条括号，避免模型叙事里的数字与实际血量对不上
        settle_bits = []
        for item in applied:
            target = self.players[item["player_id"]]
            if item.get("shield"):
                left = item.get("shield_left")
                left_txt = f"，剩余可挡 {left} 次" if left is not None else ""
                settle_bits.append(
                    f"{target.name} 的「{item['shield']}」生效，伤害被完全挡下{left_txt}"
                )
            elif item.get("heal_source"):
                settle_bits.append(
                    f"{target.name} 因「{item['heal_source']}」格挡回血 {item['hp_delta']:+d} → "
                    f"{item['hp']}/{target.hp_max}"
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
            await self._grant_inheritance(victim, killer)

        await self.broadcast_state()
        self._schedule_summary()

    async def _grant_inheritance(self, victim: Player, killer_id: Optional[str]) -> None:
        """击杀继承：选择死者一个正面能力，旧外壳与新效果分别折算。

        自伤、天灾（killer=None）、凶手已死或属性满额时不继承。
        """
        captured = victim.player_id in self.effects.death_legacies
        src = self.effects.death_legacies.pop(victim.player_id, None)
        killer = self.players.get(killer_id) if killer_id else None
        if killer is None or killer.player_id == victim.player_id or not killer.alive:
            return
        if not captured:
            src = inheritance.pick_candidate(victim, self.effects.active.values(), self.turn_no)
        if src is None:
            return
        if (len(killer.attributes) + sum(killer.player_id in effect.spec.targets
                for effect in self.effects.active.values()) >= self.pack.attributes.max_count
                or (isinstance(src, inheritance.EffectLegacy) and len(self.effects.active) >= self.effects.MAX_ACTIVE)):
            self._record(
                LedgerType.INHERITANCE,
                f"{killer.name} 想继承 {victim.name} 的「{src.name}」，但自身能力已满，只能作罢。",
                payload={
                    "kind": "inheritance",
                    "killer": killer.player_id,
                    "victim": victim.player_id,
                    "name": src.name,
                    "granted": False,
                },
            )
            return
        if isinstance(src, inheritance.EffectLegacy):
            got = self.effects.inherit(src, victim, killer, self.pack.inheritance.power_ratio)
            if got is None:
                self._record(LedgerType.INHERITANCE,
                    f"{killer.name} 未继承「{src.name}」：自身已满足该能力的结束条件。",
                    payload={"kind": "inheritance", "granted": False})
                return
            value = sum(a.amount or a.charges or 1 for a in got.spec.actions)
            self._record(LedgerType.INHERITANCE,
                f"{killer.name} 从 {victim.name} 继承了「{src.name}」；{self.effects.describe(got)}。",
                actor=killer.player_id, payload={"kind": "inheritance", "killer": killer.player_id,
                    "victim": victim.player_id, "name": got.spec.name, "source_name": src.name,
                    "source_value": src.value, "value": value, "effect_id": got.effect_id, "granted": True})
            await self._broadcast({"type": "inheritance", "killer": killer.player_id,
                "killer_name": killer.name, "victim": victim.player_id, "victim_name": victim.name,
                "name": got.spec.name, "source_name": src.name, "source_value": src.value, "value": value})
            return
        attr = inheritance.build_inherited(
            killer=killer,
            src=src,
            victim_name=victim.name,
            ratio=self.pack.inheritance.power_ratio,
            turn_no=self.turn_no,
            max_cap=self.pack.attributes.max_cap,
            extra_names=[e.spec.name for e in self.effects.active.values() if killer.player_id in e.spec.targets],
        )
        killer.attributes[attr.attr_id] = attr
        self._record(
            LedgerType.INHERITANCE,
            f"{killer.name} 从 {victim.name} 的遗骸中继承了「{src.name}」："
            f"{src.value} → {attr.value}。",
            actor=killer.player_id,
            payload={
                "kind": "inheritance",
                "killer": killer.player_id,
                "victim": victim.player_id,
                "name": attr.name,
                "source_name": src.name,
                "source_value": src.value,
                "value": attr.value,
                "granted": True,
            },
        )
        await self._broadcast(
            {
                "type": "inheritance",
                "killer": killer.player_id,
                "killer_name": killer.name,
                "victim": victim.player_id,
                "victim_name": victim.name,
                "name": attr.name,
                "source_name": src.name,
                "source_value": src.value,
                "value": attr.value,
            }
        )

    def _schedule_summary(self) -> None:
        if os.environ.get("YAP_JUDGE_AI", "1") == "0":
            return
        if self._summary_task and not self._summary_task.done():
            return
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

    async def _finish(self, alive: list[Player], reason: str = "") -> None:
        self.over = True
        self._over_since = time.time()
        self.end_reason = reason
        self.phase = Phase.OVER
        self.deadline = None
        winner = alive[0] if len(alive) == 1 else None
        if not alive and not reason and self.pack.tie_break and self.players:
            ranked = self._ranking()
            score = lambda p: tuple(getattr(p, key) for key in self.pack.tie_break)
            if len(ranked) == 1 or score(ranked[0]) != score(ranked[1]):
                winner = ranked[0]
        self.winner = winner.player_id if winner else None
        result = self.result_view() or {"winner_name": None, "ranking": []}
        self._record(
            LedgerType.SYSTEM,
            reason or (f"{winner.name} 获胜。" if winner else "平局，所有玩家全部死亡。"),
            payload={
                "kind": "game_over",
                "winner": self.winner,
                "ranking": result["ranking"],
                "reason": reason,
            },
        )
        await self._broadcast(
            {
                "type": "game_over",
                "winner": self.winner,
                "winner_name": result["winner_name"],
                "ranking": result["ranking"],
                "reason": reason,
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
        turn = self.turn_no
        await asyncio.sleep(
            BOT_THINK_MIN + (BOT_THINK_MAX - BOT_THINK_MIN) * brain.rng.random()
        )
        if (self.phase != Phase.DECLARE or self.current_actor != bot.player_id
                or self.turn_no != turn):
            return

        decl = None
        if brain.difficulty in ("hard", "inferno"):
            # 困难/炼狱用一次房主代理写宣言；超时或失败回落模板
            budget = max(5.0, min(18.0, float(self.pack.timers.declare_s) - 12.0))
            text = await judge_ai.draft_bot_action(
                bot=bot,
                players=self.players,
                turn_no=self.turn_no,
                pack=self.pack,
                ledger=self.ledger,
                world_summary=self.world_summary,
                base_url=self.judge_base_url,
                model=self.judge_model,
                timeout=budget,
                effect_context=self.effects.context(),
                allowed_targets=self.effects.allowed_targets(bot.player_id),
            )
            if text:
                decl = Declaration(actor_id=bot.player_id, content=text)
        if decl is None:
            allowed = self.effects.allowed_targets(bot.player_id) | {bot.player_id}
            candidates = {pid: p for pid, p in self.players.items() if pid in allowed}
            decl = brain.declare(bot, candidates, self.turn_no, self.pack, effects=self.effects)

        # 等待模型期间回合可能已经推进，丢弃迟到的宣言
        if (self.phase != Phase.DECLARE or self.current_actor != bot.player_id
                or self.turn_no != turn):
            return
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
