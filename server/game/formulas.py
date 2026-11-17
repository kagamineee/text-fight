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
    level_words: dict[str, float]

    def cap(self, turn_no: int) -> int:
        """软上限曲线：随回合增长的力量曲线。"""
        return max(1, int(self.cap_base + self.cap_growth * max(0, turn_no - 1)))

    def hard(self, turn_no: int) -> int:
        """硬上限：服务端强制钳制的绝对边界。"""
        return max(1, int(round(self.cap(turn_no) * self.ceiling_factor)))

    def level_ratio(self, hint: str | None) -> float | None:
        if not hint:
            return None
        return self.level_words.get(hint.strip())


def parse_intensity_hint(
    hint: str | None, rules: IntensityRules, turn_no: int, default_ratio: float
) -> float:
    """把玩家的"意图方言"解析为基准影响力。

    支持三种输入：纯数字 / 程度词 / 未提供（按动作类型默认比例）。
    注意：这只是基准值，最终仍会被 clamp_to_hard 钳制。
    """
    cap = rules.cap(turn_no)
    if hint:
        text = hint.strip()
        if text.isdigit():
            return float(int(text))
        ratio = rules.level_ratio(text)
        if ratio is not None:
            return cap * ratio
    return cap * default_ratio


def counter_factor(counter_list: list[dict]) -> float:
    """P0 简化对冲：每条有效对冲削减 15%，最多 45%。

    P1 起由裁判做语义评估，但此函数保留为兜底路径。
    """
    n = len([c for c in counter_list if c.get("kind") == "counter"])
    return max(0.55, 1.0 - 0.15 * n)


def clamp_to_hard(value: float, turn_no: int, rules: IntensityRules) -> float:
    """服务端终审：任何数值不得越过硬上限。"""
    return max(0.0, min(float(value), float(rules.hard(turn_no))))


def split_shares(total: float, count: int) -> list[float]:
    """多目标均摊，保证总和不超过 total。"""
    if count <= 0:
        return []
    share = total / count
    return [share] * count


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
