"""属性构造与校验。

属性只能由裁判根据玩家写的文本创建或修改，玩家面板是只读的。
旧的 `open_attribute` 入口保留给协议兼容，正常对局不再使用。
"""

from __future__ import annotations

import uuid

from .models import Attribute, Player

ATTR_TYPES = {"attack", "defense", "utility"}
MAX_NAME_LEN = 16
MAX_NARRATIVE_LEN = 200


class AttributeError_(Exception):
    """属性校验失败。"""


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
) -> Attribute:
    name = name.strip()[:MAX_NAME_LEN]
    value = max(1, int(value))
    cap = max(value, min(int(cap), max_cap))
    return Attribute(
        attr_id=uuid.uuid4().hex[:10],
        owner_id=owner_id,
        name=name,
        narrative=narrative.strip()[:MAX_NARRATIVE_LEN],
        attr_type=attr_type,
        initial=value,
        cap=cap,
        exchange_rate=1.0,
        consume_rule="",
        value=value,
        opened_turn=turn_no,
    )


def validate_open(me: Player, pack, payload: dict) -> None:
    name = str(payload.get("name", "")).strip()
    narrative = str(payload.get("narrative", "")).strip()
    attr_type = str(payload.get("attr_type", "")).strip()

    if not name:
        raise AttributeError_("属性名不能为空")
    if len(name) > MAX_NAME_LEN:
        raise AttributeError_(f"属性名不能超过 {MAX_NAME_LEN} 个字")
    if len(narrative) > MAX_NARRATIVE_LEN:
        raise AttributeError_(f"属性描述不能超过 {MAX_NARRATIVE_LEN} 个字")
    if attr_type not in ATTR_TYPES:
        raise AttributeError_("属性类型必须是 attack / defense / utility 之一")
    if len(me.attributes) >= pack.attributes.max_count:
        raise AttributeError_(f"每人最多 {pack.attributes.max_count} 个属性")
    if any(a.name == name for a in me.attributes.values()):
        raise AttributeError_("同名属性已存在")

    try:
        initial = int(payload.get("initial", 1))
        cap = int(payload.get("cap", 10))
        rate = float(payload.get("exchange_rate", 1.0))
    except (TypeError, ValueError) as exc:
        raise AttributeError_("初始值 / 上限 / 折算率必须是数字") from exc

    if not 1 <= initial <= pack.attributes.max_initial:
        raise AttributeError_(f"初始值必须在 1~{pack.attributes.max_initial} 之间")
    if not initial <= cap <= pack.attributes.max_cap:
        raise AttributeError_(f"上限必须在 {initial}~{pack.attributes.max_cap} 之间")
    if not 0 < rate <= pack.attributes.max_exchange_rate:
        raise AttributeError_(f"折算率必须在 0~{pack.attributes.max_exchange_rate} 之间")


def build_attribute(me: Player, payload: dict, turn_no: int) -> Attribute:
    return Attribute(
        attr_id=uuid.uuid4().hex[:10],
        owner_id=me.player_id,
        name=str(payload["name"]).strip(),
        narrative=str(payload.get("narrative", "")).strip(),
        attr_type=str(payload["attr_type"]),
        initial=int(payload["initial"]),
        cap=int(payload["cap"]),
        exchange_rate=float(payload["exchange_rate"]),
        consume_rule=str(payload.get("consume_rule", "")).strip(),
        value=int(payload["initial"]),
        opened_turn=turn_no,
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
        "consume_rule": attr.consume_rule,
        "value": attr.value,
        "opened_turn": attr.opened_turn,
    }
