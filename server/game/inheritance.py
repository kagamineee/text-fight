"""击杀继承：死者最强的属性外壳按包比例折算给凶手。

继承的是叙事外壳与重新折算后的数值；免伤、冻结、持续伤害这类状态标记不随外壳转移。
"""

from __future__ import annotations

from typing import Optional

from . import attributes as attr_mod
from .models import Attribute, Player

NAME_SUFFIX = "·遗"
MAX_NAME_LEN = attr_mod.MAX_NAME_LEN


def pick_legacy(victim: Player) -> Optional[Attribute]:
    """取死者当前数值最高的属性；同分取上限更高者。"""
    if not victim.attributes:
        return None
    return max(victim.attributes.values(), key=lambda a: (a.value, a.cap))


def scaled(src: Attribute, ratio: float) -> tuple[int, int]:
    value = max(1, int(round(src.value * ratio)))
    cap = max(value, int(round(src.cap * ratio)))
    return value, cap


def unique_name(killer: Player, base: str) -> str:
    """在凶手账户内保证名称唯一，且不超过长度上限。"""
    taken = {a.name for a in killer.attributes.values()}
    name = base[:MAX_NAME_LEN]
    if name not in taken:
        return name
    for i in range(2, 10):
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
) -> Attribute:
    value, cap = scaled(src, ratio)
    return attr_mod.create_attribute(
        owner_id=killer.player_id,
        name=unique_name(killer, src.name + NAME_SUFFIX),
        attr_type=src.attr_type,
        value=value,
        cap=cap,
        narrative=f"从{victim_name}的遗骸中夺来的「{src.name}」：力量已经衰减，归属已经易主。",
        turn_no=turn_no,
        max_cap=max_cap,
    )
