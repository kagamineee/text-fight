"""属性构造与校验。

属性只能由裁判根据玩家写的文本创建或修改，玩家面板是只读的。
"""

from __future__ import annotations

import uuid

from .models import Attribute

ATTR_TYPES = {"attack", "defense", "utility"}
MAX_NAME_LEN = 16
MAX_NARRATIVE_LEN = 200


def create_attribute(
    *,
    owner_id: str,
    name: str,
    attr_type: str,
    value: int,
    cap: int,
    narrative: str,
    turn_no: int,
    max_cap: int,
    duration: int = 0,
    shield: bool = False,
    freeze: bool = False,
    per_turn: int = 0,
    indestructible: bool = False,
    damage_reduction: int = 0,
    shield_charges: int = 0,
    lethal_only: bool = False,
    block_heal_pct: int = 0,
) -> Attribute:
    name = name.strip()[:MAX_NAME_LEN]
    value = max(1, min(int(value), max_cap))
    cap = max(value, min(int(cap), max_cap))
    duration = max(0, int(duration))
    # 冻结类按“目标自己被跳过的回合数”计费，与全局回合号脱钩：
    # duration=3 → 目标跳过 3 个自己的回合后解冻，不会提前消失。
    charges = duration if freeze else 0
    return Attribute(
        attr_id=uuid.uuid4().hex[:10],
        owner_id=owner_id,
        name=name,
        narrative=narrative.strip()[:MAX_NARRATIVE_LEN],
        attr_type=attr_type,
        initial=value,
        cap=cap,
        exchange_rate=1.0,
        value=value,
        opened_turn=turn_no,
        expires_turn=0 if freeze else (turn_no + duration - 1 if duration > 0 else 0),
        shield=bool(shield),
        freeze=bool(freeze),
        charges=charges,
        per_turn=int(per_turn),
        indestructible=bool(indestructible),
        damage_reduction=max(0, min(90, int(damage_reduction))),
        shield_charges=max(0, int(shield_charges)),
        lethal_only=bool(lethal_only),
        block_heal_pct=max(0, min(100, int(block_heal_pct))),
    )


def seal_event(attr: Attribute) -> dict:
    """封存事件载荷：完整记录，重建对局时依赖它。"""
    return {
        "attr_id": attr.attr_id,
        "owner_id": attr.owner_id,
        "name": attr.name,
        "narrative": attr.narrative,
        "attr_type": attr.attr_type,
        "initial": attr.initial,
        "cap": attr.cap,
        "exchange_rate": attr.exchange_rate,
        "value": attr.value,
        "opened_turn": attr.opened_turn,
        "expires_turn": attr.expires_turn,
        "shield": attr.shield,
        "freeze": attr.freeze,
        "charges": attr.charges,
        "per_turn": attr.per_turn,
        "indestructible": attr.indestructible,
        "damage_reduction": attr.damage_reduction,
        "shield_charges": attr.shield_charges,
        "lethal_only": attr.lethal_only,
        "block_heal_pct": attr.block_heal_pct,
    }
