"""P1 裁判管线单测：JSON 解析、结构化校验、服务端钳制。"""

from __future__ import annotations

import unittest

from server.game.judge_ai import materialize, parse_judge_json
from server.game.models import Attribute, Player
from server.packs.loader import load_packs


def make_player(pid: str, name: str, hp: int = 40) -> Player:
    return Player(
        player_id=pid,
        name=name,
        seat=0 if pid == "a" else 1,
        kind="human",
        hp=hp,
        hp_max=40,
    )


PACK = load_packs()["default-arena"]


class ParseTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        out = parse_judge_json(
            '{"narrative":"甲推了乙一把。","effects":[{"player_id":"b","hp_delta":-3}]}'
        )
        self.assertEqual(out.narrative, "甲推了乙一把。")
        self.assertEqual(out.effects[0].hp_delta, -3)

    def test_fenced_json(self) -> None:
        out = parse_judge_json(
            '```json\n{"narrative":"甲站着没动。","effects":[],"attr_changes":[]}\n```'
        )
        self.assertEqual(out.narrative, "甲站着没动。")

    def test_invalid_text(self) -> None:
        with self.assertRaises(ValueError):
            parse_judge_json("模型今天不想输出 JSON")

    def test_missing_narrative(self) -> None:
        with self.assertRaises(Exception):
            parse_judge_json('{"effects":[]}')


class MaterializeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = make_player("a", "甲")
        self.b = make_player("b", "乙")
        self.players = {"a": self.a, "b": self.b}

    def test_overdamage_is_clamped(self) -> None:
        out = parse_judge_json(
            '{"narrative":"甲把乙打飞了。","effects":[{"player_id":"b","hp_delta":-999}]}'
        )
        verdict, clamped = materialize(out, self.players, PACK, turn_no=1)
        self.assertTrue(clamped)
        self.assertEqual(verdict.effects, [{"player_id": "b", "hp_delta": -12}])

    def test_unknown_player_effect_dropped(self) -> None:
        out = parse_judge_json(
            '{"narrative":"甲打了一下空气。","effects":[{"player_id":"ghost","hp_delta":-5}]}'
        )
        verdict, clamped = materialize(out, self.players, PACK, turn_no=1)
        self.assertEqual(verdict.effects, [])
        self.assertTrue(clamped)

    def test_attr_create_is_clamped_to_pack(self) -> None:
        out = parse_judge_json(
            """{"narrative":"甲手上多了一层铁皮。",
                "attr_changes":[{"owner":"a","name":"铁皮","attr_type":"defense",
                                 "value":999,"cap":9999,"narrative":"练出来的"}]}"""
        )
        verdict, clamped = materialize(out, self.players, PACK, turn_no=1)
        self.assertTrue(clamped)
        change = verdict.attr_changes[0]
        self.assertTrue(change["created"])
        self.assertEqual(change["value"], PACK.attributes.max_initial)
        self.assertEqual(change["cap"], PACK.attributes.max_cap)

    def test_attr_update_uses_existing_cap(self) -> None:
        attr = Attribute(
            attr_id="x1",
            owner_id="a",
            name="铁皮",
            narrative="",
            attr_type="defense",
            initial=3,
            cap=6,
            exchange_rate=1.0,
            consume_rule="",
            value=3,
            opened_turn=1,
        )
        self.a.attributes[attr.attr_id] = attr
        out = parse_judge_json(
            """{"narrative":"甲的护甲裂了。",
                "attr_changes":[{"owner":"a","attr_id":"x1","delta":-10}]}"""
        )
        verdict, clamped = materialize(out, self.players, PACK, turn_no=1)
        self.assertTrue(clamped)
        self.assertEqual(verdict.attr_changes[0]["value"], 0)

    def test_attr_duration_is_kept(self) -> None:
        out = parse_judge_json(
            """{"narrative":"甲身上多了一层临时护甲。",
                "attr_changes":[{"owner":"a","name":"临时护甲","attr_type":"defense",
                                 "value":3,"cap":6,"duration":2}]}"""
        )
        verdict, clamped = materialize(out, self.players, PACK, turn_no=3)
        self.assertFalse(clamped)
        self.assertEqual(verdict.attr_changes[0]["duration"], 2)

    def test_invalid_attr_id_dropped(self) -> None:
        out = parse_judge_json(
            """{"narrative":"甲想动一个不存在的东西。",
                "attr_changes":[{"owner":"a","attr_id":"nope","delta":1}]}"""
        )
        verdict, clamped = materialize(out, self.players, PACK, turn_no=1)
        self.assertEqual(verdict.attr_changes, [])
        self.assertTrue(clamped)


if __name__ == "__main__":
    unittest.main()
