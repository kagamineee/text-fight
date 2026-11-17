"""对局运行时数据模型。

说明：这里只放内存态；持久化走事件流（server/store）。
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
    sealed_at: float = field(default_factory=time.time)

    def public_view(self) -> dict:
        """隐形层：他人只能看到名字与叙事外壳，看不到数值。"""
        return {
            "attr_id": self.attr_id,
            "name": self.name,
            "narrative": self.narrative,
            "attr_type": self.attr_type,
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
    guard_active: bool = False

    def public_view(self) -> dict:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "seat": self.seat,
            "kind": self.kind,
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
