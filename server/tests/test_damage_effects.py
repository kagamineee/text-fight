"""减伤 / 次数护盾 / 格挡回血单测。"""

import asyncio
import unittest
from unittest.mock import patch

from server.game import attributes as attr_mod
from server.game import judge_ai
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
    room = Room(room_id="r1", code="AAAAAA", password_hash="", pack=pack, host_id="", hub=NullHub())
    names = ["甲", "乙", "丙"]
    players = [room.add_player(names[i], kind="human", pid=f"p{i}") for i in range(n_players)]
    return room, players


def seed_shield(owner, *, charges=0, heal_pct=0, reduction=0, attr_type="defense", lethal_only=False):
    attr = attr_mod.create_attribute(
        owner_id=owner.player_id,
        name="圣光庇护",
        attr_type=attr_type,
        value=1,
        cap=1,
        narrative="测试用",
        turn_no=5,
        max_cap=99,
        shield=charges > 0 or heal_pct > 0,
        shield_charges=charges,
        lethal_only=lethal_only,
        block_heal_pct=heal_pct,
        damage_reduction=reduction,
    )
    owner.attributes[attr.attr_id] = attr
    return attr


def hit(room, actor, target_id, damage, attr_changes=None):
    verdict = judge_ai.judge.Verdict(
        effects=[{"player_id": target_id, "hp_delta": -damage}],
        attr_changes=attr_changes or [],
        narrative="一击",
    )
    fake = judge_ai.JudgeResult(verdict=verdict, source="ai")

    async def fake_adjudicate(**kwargs):
        return fake

    with patch.object(judge_ai, "adjudicate", new=fake_adjudicate):
        asyncio.run(room._adjudicate(actor, Declaration(actor_id=actor.player_id, content="攻击")))


class DamageReductionTest(unittest.TestCase):
    def test_half_damage(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        seed_shield(players[1], reduction=50, attr_type="utility")
        hit(room, players[0], players[1].player_id, 20)
        self.assertEqual(players[1].hp, players[1].hp_max - 10)

    def test_reduction_clamped_to_ninety(self) -> None:
        pack = load_packs()["default-arena"]
        _room, players = make_room(2)
        change = judge_ai.AttrChangeOut(
            owner=players[0].player_id,
            name="钢筋铁骨",
            attr_type="utility",
            value=1,
            cap=1,
            duration=2,
            damage_reduction=120,
        )
        changes, clamped = judge_ai._normalize_attr(
            change, {p.player_id: p for p in players}, pack, set(), 20
        )
        self.assertEqual(changes[0]["damage_reduction"], 90)
        self.assertTrue(clamped)


class ShieldChargesTest(unittest.TestCase):
    def test_charges_consumed_then_shield_gone(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        attr = seed_shield(players[1], charges=2)
        hit(room, players[0], players[1].player_id, 30)
        self.assertEqual(players[1].hp, players[1].hp_max)
        self.assertEqual(attr.shield_charges, 1)
        hit(room, players[0], players[1].player_id, 30)
        self.assertEqual(players[1].hp, players[1].hp_max)
        self.assertNotIn(attr.attr_id, players[1].attributes)

    def test_unlimited_shield_keeps_charges_zero(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        attr = seed_shield(players[1], charges=0)
        attr.shield = True
        hit(room, players[0], players[1].player_id, 30)
        self.assertIn(attr.attr_id, players[1].attributes)
        self.assertEqual(attr.shield_charges, 0)


class BlockHealTest(unittest.TestCase):
    def test_heals_all_alive_on_block(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        owner, other = players[1], players[0]
        owner.hp = 50
        other.hp = 40
        seed_shield(owner, charges=3, heal_pct=80)

        hit(room, other, owner.player_id, 30)

        self.assertEqual(owner.hp, 100)  # 50 + 80，封顶 100
        self.assertEqual(other.hp, 100)  # 40 + 80，封顶 100

    def test_heal_applies_even_when_last_charge_used(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        owner = players[1]
        owner.hp = 50
        attr = seed_shield(owner, charges=1, heal_pct=50)

        hit(room, players[0], owner.player_id, 30)

        self.assertNotIn(attr.attr_id, owner.attributes)
        self.assertEqual(owner.hp, 100)


class LethalOnlyShieldTest(unittest.TestCase):
    def test_non_lethal_hit_passes_through(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        attr = seed_shield(players[1], charges=3, lethal_only=True, attr_type="utility")
        hit(room, players[0], players[1].player_id, 30)
        self.assertEqual(players[1].hp, players[1].hp_max - 30)
        self.assertEqual(attr.shield_charges, 3)

    def test_lethal_hit_blocked_and_charge_used(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        attr = seed_shield(players[1], charges=2, lethal_only=True)
        players[1].hp = 20
        hit(room, players[0], players[1].player_id, 30)
        self.assertEqual(players[1].hp, 20)
        self.assertEqual(attr.shield_charges, 1)

    def test_normal_shield_blocks_non_lethal(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        attr = seed_shield(players[1], charges=2, lethal_only=False)
        hit(room, players[0], players[1].player_id, 30)
        self.assertEqual(players[1].hp, players[1].hp_max)
        self.assertEqual(attr.shield_charges, 1)

    def test_no_heal_on_non_lethal_hit(self) -> None:
        room, players = make_room(2)
        room.turn_no = 5
        owner = players[1]
        owner.hp = 50
        seed_shield(owner, charges=3, heal_pct=50, lethal_only=True, attr_type="utility")
        hit(room, players[0], owner.player_id, 10)
        self.assertEqual(owner.hp, 40)


class NormalizeLethalOnlyTest(unittest.TestCase):
    def test_lethal_only_passed(self) -> None:
        pack = load_packs()["default-arena"]
        _room, players = make_room(2)
        change = judge_ai.AttrChangeOut(
            owner=players[0].player_id,
            name="九命",
            attr_type="defense",
            value=1,
            cap=1,
            duration=3,
            shield_charges=3,
            lethal_only=True,
        )
        changes, _clamped = judge_ai._normalize_attr(
            change, {p.player_id: p for p in players}, pack, set(), 20
        )
        self.assertTrue(changes[0]["lethal_only"])
        self.assertTrue(changes[0]["shield"])
        self.assertEqual(changes[0]["shield_charges"], 3)


class NormalizeShieldFieldsTest(unittest.TestCase):
    def test_charges_and_heal_force_shield(self) -> None:
        pack = load_packs()["default-arena"]
        _room, players = make_room(2)
        change = judge_ai.AttrChangeOut(
            owner=players[0].player_id,
            name="圣光庇护",
            attr_type="defense",
            value=1,
            cap=1,
            duration=3,
            shield_charges=3,
            block_heal_pct=80,
        )
        changes, _clamped = judge_ai._normalize_attr(
            change, {p.player_id: p for p in players}, pack, set(), 20
        )
        self.assertTrue(changes[0]["shield"])
        self.assertEqual(changes[0]["shield_charges"], 3)
        self.assertEqual(changes[0]["block_heal_pct"], 80)


if __name__ == "__main__":
    unittest.main()
