"""「不可破坏」属性单测：他人无法移除，本人可移除，到期仍会消失。"""

import asyncio
import unittest
from unittest.mock import patch

from server.game import attributes as attr_mod
from server.game import inheritance, judge_ai
from server.game.engine import Room
from server.game.models import Declaration
from server.packs.loader import load_packs


class NullHub:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast_room(self, room_id: str, msg: dict) -> None:
        self.messages.append(msg)

    async def send_player(self, player_id: str, msg: dict) -> None:
        self.messages.append(msg)


def make_room(n_players: int = 2):
    pack = load_packs()["default-arena"]
    hub = NullHub()
    room = Room(room_id="r1", code="AAAAAA", password_hash="", pack=pack, host_id="", hub=hub)
    names = ["甲", "乙"]
    players = [room.add_player(names[i], kind="human", pid=f"p{i}") for i in range(n_players)]
    return room, players, hub


def seed_attr(owner, name: str, *, indestructible: bool = True, duration: int = 0, turn_no: int = 5):
    attr = attr_mod.create_attribute(
        owner_id=owner.player_id,
        name=name,
        attr_type="defense",
        value=1,
        cap=1,
        narrative="不可破坏的壁垒",
        turn_no=turn_no,
        max_cap=99,
        duration=duration,
        indestructible=indestructible,
    )
    owner.attributes[attr.attr_id] = attr
    return attr


def remove_verdict(owner_id: str, attr_id: str, narrative: str = "一击落下"):
    verdict = judge_ai.judge.Verdict(
        effects=[],
        attr_changes=[{"owner": owner_id, "attr_id": attr_id, "name": "", "remove": True}],
        narrative=narrative,
    )
    return judge_ai.JudgeResult(verdict=verdict, source="ai")


class IndestructibleRemoveTest(unittest.TestCase):
    def _run(self, actor_index: int, remove_index: int):
        room, players, _hub = make_room(2)
        room.turn_no = 5
        attr = seed_attr(players[remove_index], "坚不可摧")
        actor = players[actor_index]
        fake = remove_verdict(players[remove_index].player_id, attr.attr_id)

        async def fake_adjudicate(**kwargs):
            return fake

        with patch.object(judge_ai, "adjudicate", new=fake_adjudicate):
            asyncio.run(room._adjudicate(actor, Declaration(actor_id=actor.player_id, content="打碎它")))
        return room, players[remove_index], attr

    def test_foreign_remove_blocked(self) -> None:
        room, owner, attr = self._run(actor_index=0, remove_index=1)
        self.assertIn(attr.attr_id, owner.attributes)
        payloads = [e.payload for e in room.ledger if e.payload.get("kind") == "attr_indestructible"]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["owner"], owner.player_id)

    def test_owner_can_remove_own(self) -> None:
        room, owner, attr = self._run(actor_index=1, remove_index=1)
        self.assertNotIn(attr.attr_id, owner.attributes)

    def test_steal_copy_is_dropped(self) -> None:
        room, players, _hub = make_room(2)
        room.turn_no = 5
        victim = players[1]
        thief = players[0]
        attr = seed_attr(victim, "坚不可摧")
        verdict = judge_ai.judge.Verdict(
            effects=[],
            attr_changes=[
                {"owner": victim.player_id, "attr_id": attr.attr_id, "remove": True},
                {
                    "owner": thief.player_id,
                    "name": "坚不可摧",
                    "attr_type": "defense",
                    "value": 1,
                    "cap": 1,
                    "created": True,
                },
            ],
            narrative="夺取",
        )
        fake = judge_ai.JudgeResult(verdict=verdict, source="ai")

        async def fake_adjudicate(**kwargs):
            return fake

        with patch.object(judge_ai, "adjudicate", new=fake_adjudicate):
            asyncio.run(room._adjudicate(thief, Declaration(actor_id=thief.player_id, content="抢")))

        self.assertIn(attr.attr_id, victim.attributes)
        self.assertFalse(any(a.name == "坚不可摧" for a in thief.attributes.values()))


class IndestructibleExpiryTest(unittest.TestCase):
    def test_expiry_still_applies(self) -> None:
        room, players, _hub = make_room(2)
        room.turn_no = 5
        attr = seed_attr(players[0], "不灭壁垒", duration=2)  # 第 6 回合结束后到期
        self.assertEqual(attr.expires_turn, 6)

        asyncio.run(room._expire_attributes(list(players)))
        self.assertIn(attr.attr_id, players[0].attributes)

        room.turn_no = 7
        asyncio.run(room._expire_attributes(list(players)))
        self.assertNotIn(attr.attr_id, players[0].attributes)


class IndestructibleInheritanceTest(unittest.TestCase):
    def test_flag_not_inherited(self) -> None:
        _room, players, _hub = make_room(2)
        src = seed_attr(players[1], "不灭壁垒")
        attr = inheritance.build_inherited(
            killer=players[0],
            src=src,
            victim_name=players[1].name,
            ratio=0.4,
            turn_no=8,
            max_cap=99,
        )
        self.assertFalse(attr.indestructible)


class IndestructibleNormalizeTest(unittest.TestCase):
    def test_normalize_passes_flag(self) -> None:
        pack = load_packs()["default-arena"]
        _room, players, _hub = make_room(2)
        change = judge_ai.AttrChangeOut(
            owner=players[0].player_id,
            name="不灭壁垒",
            attr_type="defense",
            value=1,
            cap=1,
            duration=3,
            shield=True,
            indestructible=True,
            narrative="不可破坏",
        )
        changes, _clamped = judge_ai._normalize_attr(
            change, {p.player_id: p for p in players}, pack, set(), 20
        )
        self.assertTrue(changes[0]["indestructible"])
        self.assertTrue(changes[0]["shield"])


if __name__ == "__main__":
    unittest.main()
