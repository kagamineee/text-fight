"""P0 公式单测：软上限、硬上限、对冲、均摊、防御、底线、磨损。"""

from __future__ import annotations

import unittest

from server.game.formulas import (
    IntensityRules,
    apply_armor_wear,
    clamp_to_hard,
    counter_factor,
    effective_defense,
    parse_intensity_hint,
    resolve_damage,
    split_shares,
)

RULES = IntensityRules(
    cap_base=8,
    cap_growth=2,
    ceiling_factor=1.5,
    level_words={"微弱": 0.3, "寻常": 0.6, "强力": 1.0, "致命": 1.6},
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

    def test_parse_numeric(self) -> None:
        self.assertEqual(parse_intensity_hint("6", RULES, 1, 0.6), 6.0)
        self.assertEqual(parse_intensity_hint("999", RULES, 1, 0.6), 999.0)

    def test_parse_level_word(self) -> None:
        self.assertEqual(parse_intensity_hint("强力", RULES, 1, 0.6), 8.0)
        self.assertAlmostEqual(parse_intensity_hint("寻常", RULES, 3, 0.6), 7.2)

    def test_parse_default_ratio(self) -> None:
        self.assertAlmostEqual(parse_intensity_hint(None, RULES, 1, 0.6), 4.8)
        self.assertAlmostEqual(parse_intensity_hint("不存在", RULES, 1, 0.5), 4.0)


class ClampTests(unittest.TestCase):
    def test_clamp_to_hard(self) -> None:
        self.assertEqual(clamp_to_hard(99.0, 1, RULES), 12.0)
        self.assertEqual(clamp_to_hard(-5.0, 1, RULES), 0.0)
        self.assertEqual(clamp_to_hard(7.5, 1, RULES), 7.5)


class CounterTests(unittest.TestCase):
    def test_counter_decays(self) -> None:
        self.assertEqual(counter_factor([]), 1.0)
        self.assertAlmostEqual(counter_factor([{"kind": "counter"}]), 0.85)
        self.assertAlmostEqual(counter_factor([{"kind": "counter"}] * 3), 0.55)
        self.assertAlmostEqual(counter_factor([{"kind": "counter"}] * 9), 0.55)

    def test_ignore_non_counter(self) -> None:
        self.assertEqual(counter_factor([{"kind": "endure"}, {"kind": "ignore"}]), 1.0)


class DamageTests(unittest.TestCase):
    def test_split_shares(self) -> None:
        self.assertEqual(split_shares(12.0, 3), [4.0, 4.0, 4.0])
        self.assertEqual(split_shares(10.0, 2), [5.0, 5.0])
        self.assertEqual(split_shares(10.0, 0), [])

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
