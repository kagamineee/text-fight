"""对局运行时数据模型。

全部为内存态，不做持久化；服务重启即清空。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    LOBBY = "lobby"
    DECLARE = "declare"
    ADJUDICATE = "adjudicate"
    SETTLE = "settle"
    OVER = "over"


class LedgerType(str, Enum):
    DECLARATION = "declaration"
    ADJUDICATION = "adjudication"
    ATTRIBUTE_OPEN = "attribute_open"
    DEATH = "death"
    DISASTER = "disaster"
    INHERITANCE = "inheritance"
    APPEAL = "appeal"
    PRECEDENT = "precedent"
    SYSTEM = "system"


@dataclass
class Attribute:
    attr_id: str
    owner_id: str
    name: str
    narrative: str
    attr_type: str  # attack | defense | utility
    initial: int
    cap: int
    exchange_rate: float
    consume_rule: str
    value: int
    opened_turn: int
    expires_turn: int = 0  # 0 表示永久；大于 0 表示该回合结束后删除
    shield: bool = False  # 免伤类能力：存在即免疫一切 HP 伤害，直到被破坏 / 到期
    freeze: bool = False  # 冻结类能力：存在期间所有者无法行动，直到被解除 / 到期
    charges: int = 0  # 冻结剩余跳过次数：每次被跳过一个自己的回合扣 1，归零即消失
    per_turn: int = 0  # 周期效果：每回合开始自动结算的 HP 变化（负=毒发，正=回复）
    sealed_at: float = field(default_factory=time.time)

    def public_view(self) -> dict:
        """公开视图：所有玩家都能看到数值、上限和剩余时限。"""
        return {
            "attr_id": self.attr_id,
            "name": self.name,
            "narrative": self.narrative,
            "attr_type": self.attr_type,
            "value": self.value,
            "cap": self.cap,
            "expires_turn": self.expires_turn,
            "shield": self.shield,
            "freeze": self.freeze,
            "charges": self.charges,
            "per_turn": self.per_turn,
        }

    def private_view(self) -> dict:
        return {
            "attr_id": self.attr_id,
            "name": self.name,
            "narrative": self.narrative,
            "attr_type": self.attr_type,
            "value": self.value,
            "cap": self.cap,
            "initial": self.initial,
            "exchange_rate": self.exchange_rate,
            "consume_rule": self.consume_rule,
            "opened_turn": self.opened_turn,
            "expires_turn": self.expires_turn,
            "shield": self.shield,
            "freeze": self.freeze,
            "charges": self.charges,
            "per_turn": self.per_turn,
        }


@dataclass
class Player:
    player_id: str
    name: str
    seat: int
    kind: str  # human | bot
    hp: int
    hp_max: int
    alive: bool = True
    connected: bool = True
    ready: bool = False
    appeals_left: int = 2
    attributes: dict[str, Attribute] = field(default_factory=dict)
    hp_peak: int = 0
    survived_turns: int = 0
    bot_difficulty: str = ""

    def public_view(self) -> dict:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "seat": self.seat,
            "kind": self.kind,
            "bot_difficulty": self.bot_difficulty,
            "hp": self.hp,
            "hp_max": self.hp_max,
            "alive": self.alive,
            "connected": self.connected,
            "ready": self.ready,
            "appeals_left": self.appeals_left,
            "attributes": [a.public_view() for a in self.attributes.values()],
        }

    def private_view(self) -> dict:
        data = self.public_view()
        data["attributes"] = [a.private_view() for a in self.attributes.values()]
        return data


@dataclass
class LedgerEntry:
    seq: int
    turn_no: int
    actor: Optional[str]
    type: str
    content: str
    intensity: Optional[float] = None
    status: str = "active"
    payload: dict = field(default_factory=dict)

    def view(self) -> dict:
        return {
            "seq": self.seq,
            "turn_no": self.turn_no,
            "actor": self.actor,
            "type": self.type,
            "content": self.content,
            "intensity": self.intensity,
            "status": self.status,
        }


@dataclass
class Declaration:
    actor_id: str
    content: str
