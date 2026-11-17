"""公式单测：软上限、硬上限、钳制、防御、底线、磨损。"""

from __future__ import annotations

import unittest

from server.game.formulas import (
    IntensityRules,
    apply_armor_wear,
    clamp_attr_value,
    clamp_hp_effects,
    clamp_new_attr,
    clamp_to_hard,
    effective_defense,
    resolve_damage,
)

RULES = IntensityRules(
    cap_base=8,
    cap_growth=2,
    ceiling_factor=1.5,
)


class FakeAttr:
    def __init__(self, attr_id: str, value: int, rate: float = 1.0, kind: str = "defense"):
        self.attr_id = attr_id
        self.name = attr_id
        self.value = value
        self.exchange_rate = rate
        self.cap = 30
        self.attr_type = kind


class IntensityTests(unittest.TestCase):
    def test_cap_grows_with_turn(self) -> None:
        self.assertEqual(RULES.cap(1), 8)
        self.assertEqual(RULES.cap(3), 12)
        self.assertEqual(RULES.cap(0), 8)

    def test_hard_ceiling(self) -> None:
        self.assertEqual(RULES.hard(1), 12)
        self.assertEqual(RULES.hard(3), 18)


class ClampTests(unittest.TestCase):
    def test_clamp_to_hard(self) -> None:
        self.assertEqual(clamp_to_hard(99.0, 1, RULES), 12.0)
        self.assertEqual(clamp_to_hard(-5.0, 1, RULES), 0.0)
        self.assertEqual(clamp_to_hard(7.5, 1, RULES), 7.5)

    def test_single_overdamage_is_clamped_to_hard(self) -> None:
        effects, clamped = clamp_hp_effects(
            [{"player_id": "a", "hp_delta": -999}],
            {"a": 40},
            {"a": 40},
            hard=12,
        )
        self.assertEqual(effects, [{"player_id": "a", "hp_delta": -12}])
        self.assertTrue(clamped)

    def test_damage_cannot_exceed_remaining_hp(self) -> None:
        effects, clamped = clamp_hp_effects(
            [{"player_id": "a", "hp_delta": -9}],
            {"a": 4},
            {"a": 40},
            hard=12,
        )
        self.assertEqual(effects, [{"player_id": "a", "hp_delta": -4}])
        self.assertTrue(clamped)

    def test_total_damage_budget(self) -> None:
        effects, clamped = clamp_hp_effects(
            [
                {"player_id": "a", "hp_delta": -10},
                {"player_id": "b", "hp_delta": -10},
            ],
            {"a": 40, "b": 40},
            {"a": 40, "b": 40},
            hard=12,
        )
        self.assertEqual([e["hp_delta"] for e in effects], [-10, -2])
        self.assertTrue(clamped)

    def test_heal_cannot_exceed_gap(self) -> None:
        effects, clamped = clamp_hp_effects(
            [{"player_id": "a", "hp_delta": 999}],
            {"a": 37},
            {"a": 40},
            hard=12,
        )
        self.assertEqual(effects, [{"player_id": "a", "hp_delta": 3}])
        self.assertTrue(clamped)

    def test_unknown_player_dropped(self) -> None:
        effects, clamped = clamp_hp_effects(
            [{"player_id": "ghost", "hp_delta": -5}], {"a": 40}, {"a": 40}, hard=12
        )
        self.assertEqual(effects, [])
        self.assertTrue(clamped)

    def test_legal_effects_untouched(self) -> None:
        effects, clamped = clamp_hp_effects(
            [{"player_id": "a", "hp_delta": -5}, {"player_id": "b", "hp_delta": 2}],
            {"a": 40, "b": 30},
            {"a": 40, "b": 40},
            hard=12,
        )
        self.assertEqual(
            effects,
            [{"player_id": "a", "hp_delta": -5}, {"player_id": "b", "hp_delta": 2}],
        )
        self.assertFalse(clamped)

    def test_attr_bounds(self) -> None:
        self.assertEqual(clamp_attr_value(-3, 8), 0)
        self.assertEqual(clamp_attr_value(99, 8), 8)
        self.assertEqual(clamp_new_attr(99, 999, max_initial=10, max_cap=30), (10, 30))
        self.assertEqual(clamp_new_attr(0, 0, max_initial=10, max_cap=30), (1, 1))


class DefenseTests(unittest.TestCase):
    def test_effective_defense(self) -> None:
        attrs = [FakeAttr("a", 3, 2.0), FakeAttr("b", 0, 5.0), FakeAttr("c", 4, 1.0, "attack")]
        total, used = effective_defense(attrs)
        self.assertEqual(total, 6.0)
        self.assertEqual([a.attr_id for a in used], ["a"])

    def test_floor_damage_is_one(self) -> None:
        self.assertEqual(resolve_damage(5.0, 99.0), 1)
        self.assertEqual(resolve_damage(10.0, 3.0), 7)

    def test_armor_wear(self) -> None:
        attrs = [FakeAttr("a", 2), FakeAttr("b", 1)]
        changes = apply_armor_wear(attrs, absorbed=5)
        self.assertEqual([c["value"] for c in changes], [1, 0])
        self.assertEqual(changes[1]["broken"], True)
        self.assertEqual(apply_armor_wear(attrs, absorbed=0), [])


if __name__ == "__main__":
    unittest.main()
