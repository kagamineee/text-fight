"""继承一个正面候选：旧属性保留外壳折算，通用效果保留可独立转移的机制。"""

from __future__ import annotations

from typing import Optional
from dataclasses import dataclass
from copy import deepcopy

from . import attributes as attr_mod
from .models import Attribute, Player
from .effect_plan import EffectSpec

NAME_SUFFIX = "·遗"
MAX_NAME_LEN = attr_mod.MAX_NAME_LEN


def pick_legacy(victim: Player, turn_no: int = 0) -> Optional[Attribute]:
    """负面或正负混合属性整体排除，不把诅咒洗成正面外壳。"""
    eligible = [a for a in victim.attributes.values()
                if a.value > 0 and not a.freeze and a.per_turn >= 0
                and (not a.expires_turn or a.expires_turn >= turn_no)]
    return max(eligible, key=lambda a: (a.value, a.cap), default=None)


@dataclass
class EffectLegacy:
    name: str
    value: int
    cap: int
    spec: EffectSpec
    remaining: int | None
    round_waiting: set[str]
    opened_turn: int


def pick_candidate(victim, effects, turn_no):
    """取一个最强的安全候选；通用效果仅转移受益者自己的独立正面能力。"""
    candidates = []
    legacy = pick_legacy(victim, turn_no)
    if legacy is not None:
        candidates.append(deepcopy(legacy))
    for effect in effects:
        spec = effect.spec
        if victim.player_id not in spec.targets or spec.selection != "all":
            continue
        if effect.remaining is not None and effect.remaining <= 0:
            continue
        if spec.trigger not in ("passive", "global_start", "owner_start", "after_damage", "after_block"):
            continue
        refs = [spec.trigger_player, spec.lifetime.clock_player]
        refs += [pid for cond in (spec.condition, spec.end_condition) if cond for pid in cond.players]
        if any(pid and pid != victim.player_id for pid in refs):
            continue
        if any(a.kind not in ("armor", "reduction", "shield", "heal") for a in spec.actions):
            continue
        clone = spec.model_copy(deep=True)
        score = 0
        actions = []
        for i, action in enumerate(clone.actions):
            if action.kind == "shield" and action.charges is not None:
                action.charges = effect.charges.get(f"{victim.player_id}:{i}", 0)
                if action.charges <= 0:
                    continue
            score += action.amount or action.charges or 1
            actions.append(action)
        if not actions:
            continue
        clone.actions = actions
        candidates.append(EffectLegacy(spec.name, score, score, clone, effect.remaining,
                                       set(effect.round_waiting), effect.opened_turn))
    return max(candidates, key=lambda a: (a.value, a.cap), default=None)


def transfer_effect(src, killer, ratio, names):
    spec = src.spec.model_copy(deep=True)
    spec.name = unique_name(killer, src.name + NAME_SUFFIX, names)
    spec.local_id = None
    spec.targets = [killer.player_id]
    if spec.trigger_player:
        spec.trigger_player = killer.player_id
    if spec.lifetime.clock_player:
        spec.lifetime.clock_player = killer.player_id
    for cond in (spec.condition, spec.end_condition):
        if cond:
            cond.players = [killer.player_id]
    if src.remaining is not None:
        spec.lifetime.count = src.remaining
    for action in spec.actions:
        if action.amount is not None:
            action.amount = max(1, round(action.amount * ratio))
        if action.charges is not None:
            action.charges = max(1, round(action.charges * ratio))
    return spec


def scaled(src: Attribute, ratio: float) -> tuple[int, int]:
    value = max(1, int(round(src.value * ratio)))
    cap = max(value, int(round(src.cap * ratio)))
    return value, cap


def unique_name(killer: Player, base: str, extra_names=()) -> str:
    """在凶手账户内保证名称唯一，且不超过长度上限。"""
    taken = {a.name for a in killer.attributes.values()} | set(extra_names)
    name = base[:MAX_NAME_LEN]
    if name not in taken:
        return name
    for i in range(2, len(taken) + 3):
        suffix = f"·{i}"
        name = base[: MAX_NAME_LEN - len(suffix)] + suffix
        if name not in taken:
            return name
    return base[: MAX_NAME_LEN - 2] + "·X"


def build_inherited(
    *,
    killer: Player,
    src: Attribute,
    victim_name: str,
    ratio: float,
    turn_no: int,
    max_cap: int,
    extra_names=(),
) -> Attribute:
    value, cap = scaled(src, ratio)
    return attr_mod.create_attribute(
        owner_id=killer.player_id,
        name=unique_name(killer, src.name + NAME_SUFFIX, extra_names),
        attr_type=src.attr_type,
        value=value,
        cap=cap,
        narrative=f"从{victim_name}的遗骸中夺来的「{src.name}」：力量已经衰减，归属已经易主。",
        turn_no=turn_no,
        max_cap=max_cap,
    )
