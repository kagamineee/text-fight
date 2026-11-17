"""数值公式：全部为纯函数，可独立单测。

设计红线（PRD §5.4）：
- 模型出数，服务终审。任何来自裁判的数值都必须经过本模块钳制。
- 公式中不出现任何"平衡常数"，全部由扩展包注入。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntensityRules:
    cap_base: int
    cap_growth: int
    ceiling_factor: float

    def cap(self, turn_no: int) -> int:
        """软上限曲线：随回合增长的力量曲线。"""
        return max(1, int(self.cap_base + self.cap_growth * max(0, turn_no - 1)))

    def hard(self, turn_no: int) -> int:
        """硬上限：服务端强制钳制的绝对边界。"""
        return max(1, int(round(self.cap(turn_no) * self.ceiling_factor)))


def clamp_to_hard(value: float, turn_no: int, rules: IntensityRules) -> float:
    """服务端终审：任何数值不得越过硬上限。"""
    return max(0.0, min(float(value), float(rules.hard(turn_no))))


def effective_defense(attributes: list) -> tuple[float, list]:
    """防御总当量，以及参与抵扣的属性列表（供磨损结算使用）。"""
    total = 0.0
    used = []
    for attr in attributes:
        if not getattr(attr, "attr_type", None) == "defense":
            continue
        if attr.value <= 0:
            continue
        total += attr.value * attr.exchange_rate
        used.append(attr)
    return total, used


def resolve_damage(share: float, defense: float) -> int:
    """底线：至少造成 1 点伤害，杜绝"我毫发无伤"。"""
    return max(1, int(round(share - defense)))


def clamp_hp_effects(
    effects: list[dict],
    hp: dict[str, int],
    hp_max: dict[str, int],
    hard: int,
) -> tuple[list[dict], bool]:
    """服务端终审：把裁判给出的 HP 变化压回合法范围。

    - 单项伤害不超过目标剩余 HP，治疗不超过缺口
    - 本回合总伤害、总治疗分别不超过 hard(t)
    - 未知玩家直接丢弃
    返回 (钳制后的效果, 是否发生过钳制)。
    """
    current = dict(hp)
    out: list[dict] = []
    clamped = False
    damage_budget = max(0, int(hard))
    heal_budget = max(0, int(hard))

    for effect in effects:
        pid = str(effect.get("player_id", ""))
        if pid not in current:
            clamped = True
            continue
        try:
            delta = int(effect.get("hp_delta", 0))
        except (TypeError, ValueError):
            clamped = True
            continue
        if delta == 0:
            continue
        if delta < 0:
            allowed = min(-delta, damage_budget, max(0, current[pid]))
            if allowed <= 0:
                clamped = True
                continue
            if allowed != -delta:
                clamped = True
            damage_budget -= allowed
            current[pid] -= allowed
            entry = {"player_id": pid, "hp_delta": -allowed}
            if effect.get("pierce"):
                entry["pierce"] = True
            out.append(entry)
        else:
            gap = max(0, int(hp_max.get(pid, current[pid])) - current[pid])
            allowed = min(delta, heal_budget, gap)
            if allowed <= 0:
                clamped = True
                continue
            if allowed != delta:
                clamped = True
            heal_budget -= allowed
            current[pid] += allowed
            out.append({"player_id": pid, "hp_delta": allowed})
    return out, clamped


def clamp_attr_value(value: int, cap: int) -> int:
    """属性现值永远落在 [0, cap]。"""
    return max(0, min(int(cap), int(value)))


def clamp_new_attr(
    value: int,
    cap: int,
    max_initial: int,
    max_cap: int,
) -> tuple[int, int]:
    """新建属性的数值边界：value ∈ [1, max_initial]，cap ∈ [value, max_cap]。"""
    value = max(1, min(int(max_initial), int(value)))
    cap = max(value, min(int(max_cap), int(cap)))
    return value, cap


def apply_armor_wear(used_attrs: list, absorbed: float) -> list[dict]:
    """护甲磨损：每次吸收伤害，参与抵扣的防御属性各减 1，最低到 0。

    返回给玩家的私有变更记录（隐形层）。
    """
    changes = []
    if absorbed <= 0:
        return changes
    for attr in used_attrs:
        if attr.value <= 0:
            continue
        attr.value -= 1
        changes.append(
            {
                "attr_id": attr.attr_id,
                "name": attr.name,
                "value": attr.value,
                "cap": attr.cap,
                "broken": attr.value <= 0,
            }
        )
    return changes
