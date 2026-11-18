"""P2 机制单测：天灾曲线/结算、击杀继承。"""

import asyncio
import unittest

from server.game import attributes as attr_mod
from server.game import disaster, inheritance
from server.game.engine import Room
from server.game.models import LedgerType
from server.packs.loader import load_packs


class NullHub:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast_room(self, room_id: str, msg: dict) -> None:
        self.messages.append(msg)

    async def send_player(self, player_id: str, msg: dict) -> None:
        self.messages.append(msg)


def make_room(n_players: int = 4):
    pack = load_packs()["default-arena"]
    hub = NullHub()
    room = Room(
        room_id="r1",
        code="AAAAAA",
        password_hash="",
        pack=pack,
        host_id="",
        hub=hub,
    )
    names = ["甲", "乙", "丙", "丁"]
    players = [room.add_player(names[i], kind="human", pid=f"p{i}") for i in range(n_players)]
    return room, players, hub


class DisasterCurveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_packs()["default-arena"]

    def test_level_and_damage(self) -> None:
        self.assertEqual(disaster.level_for(self.pack, 14), 0)
        self.assertEqual(disaster.damage_for(self.pack, 0), 0)
        self.assertEqual(disaster.level_for(self.pack, 15), 1)
        self.assertEqual(disaster.damage_for(self.pack, 1), 2)
        self.assertEqual(disaster.level_for(self.pack, 17), 1)
        self.assertEqual(disaster.level_for(self.pack, 18), 2)
        self.assertEqual(disaster.damage_for(self.pack, 2), 3)
        self.assertEqual(disaster.level_for(self.pack, 21), 3)
        self.assertEqual(disaster.damage_for(self.pack, 3), 4)


class DisasterRunTest(unittest.TestCase):
    def test_damage_kill_and_no_inheritance(self) -> None:
        room, players, hub = make_room(4)
        room.turn_no = 18  # 2 级，3 点
        players[3].hp = 3

        alive = asyncio.run(room._run_disaster(list(players)))

        self.assertEqual(len(alive), 3)
        self.assertEqual(players[0].hp, 97)
        self.assertFalse(players[3].alive)
        types = [e.type for e in room.ledger]
        self.assertIn(LedgerType.DISASTER.value, types)
        self.assertIn(LedgerType.DEATH.value, types)
        self.assertNotIn(LedgerType.INHERITANCE.value, types)

        dead = [m for m in hub.messages if m.get("type") == "player_dead"]
        self.assertEqual(len(dead), 1)
        self.assertIsNone(dead[0]["killer"])
        self.assertEqual(dead[0]["cause"], "disaster")
        self.assertEqual(dead[0]["player_id"], "p3")

    def test_total_wipe(self) -> None:
        room, players, _ = make_room(2)
        room.turn_no = 15  # 1 级，2 点
        for p in players:
            p.hp = 2
        alive = asyncio.run(room._run_disaster(list(players)))
        self.assertEqual(alive, [])
        self.assertEqual(len([e for e in room.ledger if e.type == LedgerType.DEATH.value]), 2)

    def test_before_start_turn_is_noop(self) -> None:
        room, players, _ = make_room(2)
        room.turn_no = 10
        alive = asyncio.run(room._run_disaster(list(players)))
        self.assertEqual(len(alive), 2)
        self.assertEqual(players[0].hp, players[0].hp_max)
        self.assertNotIn(LedgerType.DISASTER.value, [e.type for e in room.ledger])


class InheritanceTest(unittest.TestCase):
    def _give_attr(self, owner, value: int = 10, cap: int = 20, name: str = "烈焰之心"):
        attr = attr_mod.create_attribute(
            owner_id=owner.player_id,
            name=name,
            attr_type="attack",
            value=value,
            cap=cap,
            narrative="测试属性",
            turn_no=3,
            max_cap=999,
        )
        owner.attributes[attr.attr_id] = attr
        return attr

    def test_auto_inherit_scaled(self) -> None:
        room, players, hub = make_room(3)
        killer, victim = players[0], players[1]
        self._give_attr(victim, value=10, cap=20)

        asyncio.run(room._grant_inheritance(victim, killer.player_id))

        self.assertEqual(len(killer.attributes), 1)
        got = next(iter(killer.attributes.values()))
        self.assertEqual((got.value, got.cap), (4, 8))
        self.assertTrue(got.name.endswith(inheritance.NAME_SUFFIX))
        self.assertEqual(
            [e.type for e in room.ledger if e.type == LedgerType.INHERITANCE.value],
            [LedgerType.INHERITANCE.value],
        )
        msgs = [m for m in hub.messages if m.get("type") == "inheritance"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["value"], 4)
        self.assertEqual(msgs[0]["source_value"], 10)

    def test_suicide_does_not_inherit(self) -> None:
        room, players, _ = make_room(2)
        victim = players[0]
        self._give_attr(victim)
        asyncio.run(room._grant_inheritance(victim, victim.player_id))
        self.assertEqual(len(victim.attributes), 1)
        self.assertNotIn(LedgerType.INHERITANCE.value, [e.type for e in room.ledger])

    def test_dead_killer_does_not_inherit(self) -> None:
        room, players, _ = make_room(2)
        killer, victim = players[0], players[1]
        self._give_attr(victim)
        killer.alive = False
        asyncio.run(room._grant_inheritance(victim, killer.player_id))
        self.assertEqual(killer.attributes, {})
        self.assertNotIn(LedgerType.INHERITANCE.value, [e.type for e in room.ledger])

    def test_full_attributes_rejects_inheritance(self) -> None:
        room, players, _ = make_room(2)
        killer, victim = players[0], players[1]
        self._give_attr(victim)
        for i in range(room.pack.attributes.max_count):
            self._give_attr(killer, value=1, cap=1, name=f"占位{i}")
        asyncio.run(room._grant_inheritance(victim, killer.player_id))
        self.assertEqual(len(killer.attributes), room.pack.attributes.max_count)
        inh = [e for e in room.ledger if e.type == LedgerType.INHERITANCE.value]
        self.assertEqual(len(inh), 1)
        self.assertFalse(inh[0].payload["granted"])

    def test_picks_strongest_attribute(self) -> None:
        room, players, _ = make_room(2)
        killer, victim = players[0], players[1]
        self._give_attr(victim, value=3, cap=10, name="小火花")
        self._give_attr(victim, value=12, cap=15, name="烈焰之心")
        asyncio.run(room._grant_inheritance(victim, killer.player_id))
        got = next(iter(killer.attributes.values()))
        self.assertTrue(got.name.startswith("烈焰之心"))


if __name__ == "__main__":
    unittest.main()
