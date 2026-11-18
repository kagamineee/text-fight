"""特殊事件：由扩展包定义、引擎在每回合开始硬结算的环境事件。

一个事件包含三部分：
- 触发条件：指定回合 / 固定间隔，可附加概率、冷却与次数上限
- 目标选择：全场 / 随机一人 / 血量或血量比例最高最低者
- 效果：固定伤害、按最大生命百分比变化、授予一个属性模板

本模块只做纯逻辑（判定、选目标、构造属性、渲染文案），落地与广播在 Room._run_events。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..packs.loader import EventAttrCfg, EventCfg, Pack
from . import attributes as attr_mod
from .models import Attribute, Player


@dataclass
class EventMemory:
    """单个事件在本房间内的触发记录。"""

    times: int = 0
    last_turn: int = 0


def should_fire(
    event: EventCfg,
    turn_no: int,
    memory: EventMemory,
    rng: random.Random,
) -> bool:
    """本回合是否触发；判定顺序：时机 → 上限 → 冷却 → 概率。"""
    trig = event.trigger
    if turn_no < trig.start_turn:
        return False
    if trig.max_times and memory.times >= trig.max_times:
        return False
    if trig.cooldown and memory.last_turn and turn_no - memory.last_turn <= trig.cooldown:
        return False
    scheduled = turn_no in trig.turns
    if not scheduled and trig.every > 0:
        scheduled = (turn_no - trig.start_turn) % trig.every == 0
    if not scheduled:
        return False
    if trig.chance < 1.0 and rng.random() > trig.chance:
        return False
    return True


def pick_targets(
    event: EventCfg, alive: list[Player], rng: random.Random
) -> list[Player]:
    """按 target 选人；同分时随机取一人，避免固定针对同一座位。"""
    if not alive:
        return []
    if event.target == "all":
        return list(alive)
    if event.target == "random":
        return [rng.choice(alive)]

    if event.target in ("lowest_hp", "highest_hp"):
        values = [(p, p.hp) for p in alive]
    else:  # lowest_ratio / highest_ratio
        values = [(p, p.hp / max(1, p.hp_max)) for p in alive]
    best = min(v for _, v in values) if event.target.startswith("lowest") else max(
        v for _, v in values
    )
    pool = [p for p, v in values if v == best]
    return [rng.choice(pool)]


def hp_delta(event: EventCfg, player: Player) -> int:
    """固定伤害 + 最大生命百分比，正数为回血。"""
    delta = event.hp
    if event.hp_pct:
        delta += int(round(player.hp_max * event.hp_pct / 100))
    return delta


def build_attribute(
    cfg: EventAttrCfg, owner: Player, turn_no: int, max_cap: int
) -> Attribute:
    cap = cfg.cap if cfg.cap is not None else max(cfg.value, cfg.value * 2)
    return attr_mod.create_attribute(
        owner_id=owner.player_id,
        name=cfg.name,
        attr_type=cfg.attr_type,
        value=cfg.value,
        cap=cap,
        narrative=cfg.narrative,
        turn_no=turn_no,
        max_cap=max_cap,
        duration=cfg.duration,
        shield=cfg.shield,
        freeze=cfg.freeze,
        per_turn=cfg.per_turn,
        indestructible=cfg.indestructible,
        damage_reduction=cfg.damage_reduction,
        shield_charges=cfg.shield_charges,
        lethal_only=cfg.lethal_only,
        block_heal_pct=cfg.block_heal_pct,
    )


def render(event: EventCfg, targets: list[Player]) -> str:
    """渲染文案：{target} 替换为受影响的玩家名，{name} 替换为事件名。"""
    text = event.narrative.strip()
    if not text:
        return f"特殊事件「{event.name}」发生。"
    names = "、".join(p.name for p in targets) or "无人"
    return text.replace("{target}", names).replace("{name}", event.name)


def can_grant(pack: Pack, cfg: EventAttrCfg, player: Player) -> bool:
    """属性额度与重名检查；不满足时事件仍结算伤害，只是不发属性。"""
    if len(player.attributes) >= pack.attributes.max_count:
        return False
    return all(a.name != cfg.name for a in player.attributes.values())
