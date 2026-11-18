"""特殊事件单测：触发条件、目标选择、结算落地与包校验。"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.game import attributes as attr_mod
from server.game import events, judge, judge_ai
from server.game.engine import Room
from server.game.models import LedgerType
from server.packs.loader import EventCfg, PackRegistry, load_packs


class NullHub:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast_room(self, room_id: str, msg: dict) -> None:
        self.messages.append(msg)

    async def send_player(self, player_id: str, msg: dict) -> None:
        self.messages.append(msg)


class StubRng:
    """可控随机源：random() 固定，choice() 固定取第 N 个。"""

    def __init__(self, roll: float = 0.0, pick: int = 0) -> None:
        self.roll = roll
        self.pick = pick

    def random(self) -> float:
        return self.roll

    def choice(self, seq):
        return seq[self.pick % len(seq)]


def make_event(**overrides) -> EventCfg:
    data = {
        "id": "sandstorm",
        "name": "沙暴",
        "narrative": "{target} 被沙暴割伤。",
        "trigger": {"turns": [3]},
        "target": "all",
        "hp": -5,
    }
    data.update(overrides)
    return EventCfg.model_validate(data)


def make_room(pack, n_players: int = 3):
    hub = NullHub()
    room = Room(
        room_id="r1",
        code="AAAAAA",
        password_hash="",
        pack=pack,
        host_id="",
        hub=hub,
    )
    players = [
        room.add_player(["甲", "乙", "丙", "丁"][i], kind="human", pid=f"p{i}")
        for i in range(n_players)
    ]
    return room, players, hub


def pack_with(*event_cfgs: EventCfg):
    return load_packs()["default-arena"].model_copy(update={"events": list(event_cfgs)})


class TriggerTest(unittest.TestCase):
    def test_turns_and_every(self) -> None:
        ev = make_event(trigger={"turns": [3, 9], "every": 2, "start_turn": 4})
        rng = StubRng()
        fired = [
            t
            for t in range(1, 11)
            if events.should_fire(ev, t, events.EventMemory(), rng)
        ]
        self.assertEqual(fired, [4, 6, 8, 9, 10])

    def test_chance(self) -> None:
        ev = make_event(trigger={"turns": [3], "chance": 0.5})
        self.assertFalse(
            events.should_fire(ev, 3, events.EventMemory(), StubRng(roll=0.9))
        )
        self.assertTrue(
            events.should_fire(ev, 3, events.EventMemory(), StubRng(roll=0.1))
        )

    def test_cooldown(self) -> None:
        ev = make_event(trigger={"every": 1, "start_turn": 1, "cooldown": 2})
        mem = events.EventMemory(times=1, last_turn=1)
        rng = StubRng()
        self.assertFalse(events.should_fire(ev, 2, mem, rng))
        self.assertFalse(events.should_fire(ev, 3, mem, rng))
        self.assertTrue(events.should_fire(ev, 4, mem, rng))

    def test_max_times(self) -> None:
        ev = make_event(trigger={"every": 1, "start_turn": 1, "max_times": 2})
        mem = events.EventMemory()
        rng = StubRng()
        self.assertTrue(events.should_fire(ev, 1, mem, rng))
        mem.times = 2
        self.assertFalse(events.should_fire(ev, 3, mem, rng))


class TargetTest(unittest.TestCase):
    def test_all_and_random(self) -> None:
        room, players, _ = make_room(pack_with())
        self.assertEqual(
            events.pick_targets(make_event(target="all"), players, StubRng()),
            players,
        )
        picked = events.pick_targets(make_event(target="random"), players, StubRng(pick=1))
        self.assertEqual(picked, [players[1]])

    def test_lowest_and_highest(self) -> None:
        room, players, _ = make_room(pack_with())
        players[0].hp = 50
        players[1].hp = 10
        players[2].hp = 30
        self.assertEqual(
            events.pick_targets(make_event(target="lowest_hp"), players, StubRng()),
            [players[1]],
        )
        self.assertEqual(
            events.pick_targets(make_event(target="highest_hp"), players, StubRng()),
            [players[0]],
        )

    def test_ratio_uses_max_hp(self) -> None:
        room, players, _ = make_room(pack_with())
        players[0].hp_max, players[0].hp = 200, 100  # 50%
        players[1].hp_max, players[1].hp = 100, 80  # 80%
        players[2].hp = 20  # 20%
        self.assertEqual(
            events.pick_targets(make_event(target="lowest_ratio"), players, StubRng()),
            [players[2]],
        )
        self.assertEqual(
            events.pick_targets(make_event(target="highest_ratio"), players, StubRng()),
            [players[1]],
        )


class RunEventsTest(unittest.TestCase):
    def test_damage_ledger_and_broadcast(self) -> None:
        pack = pack_with(make_event(trigger={"turns": [3]}, hp=-5))
        room, players, hub = make_room(pack)
        room.turn_no = 3

        alive = asyncio.run(room._run_events(list(players)))

        self.assertEqual(len(alive), 3)
        self.assertEqual([p.hp for p in players], [95, 95, 95])
        self.assertEqual(
            [e.type for e in room.ledger if e.type == LedgerType.EVENT.value],
            [LedgerType.EVENT.value],
        )
        msgs = [m for m in hub.messages if m.get("type") == "special_event"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["name"], "沙暴")
        self.assertEqual(len(msgs[0]["effects"]), 3)
        self.assertEqual(
            len([m for m in hub.messages if m.get("type") == "hp_update"]), 3
        )

    def test_no_fire_before_trigger_turn(self) -> None:
        pack = pack_with(make_event(trigger={"turns": [3]}))
        room, players, hub = make_room(pack)
        room.turn_no = 2

        asyncio.run(room._run_events(list(players)))

        self.assertEqual([p.hp for p in players], [100, 100, 100])
        self.assertNotIn(LedgerType.EVENT.value, [e.type for e in room.ledger])
        self.assertEqual(
            [m for m in hub.messages if m.get("type") == "special_event"], []
        )

    def test_attribute_granted_and_duplicate_skipped(self) -> None:
        ev = make_event(
            trigger={"turns": [3, 4]},
            target="random",
            hp=0,
            attribute={
                "name": "困兽之斗",
                "attr_type": "attack",
                "value": 4,
                "cap": 10,
                "duration": 2,
            },
        )
        room, players, _ = make_room(pack_with(ev), n_players=2)
        room.rng = StubRng()
        room.turn_no = 3
        asyncio.run(room._run_events(list(players)))

        target = players[0]
        self.assertEqual(len(target.attributes), 1)
        attr = next(iter(target.attributes.values()))
        self.assertEqual(attr.name, "困兽之斗")
        self.assertEqual((attr.value, attr.cap), (4, 10))
        self.assertEqual(attr.expires_turn, 4)

        room.turn_no = 4
        asyncio.run(room._run_events(list(players)))
        self.assertEqual(len(target.attributes), 1)

    def test_attribute_skipped_when_full(self) -> None:
        ev = make_event(
            trigger={"turns": [3]},
            target="all",
            hp=0,
            attribute={"name": "临阵顿悟", "value": 2},
        )
        room, players, _ = make_room(pack_with(ev), n_players=2)
        for i in range(room.pack.attributes.max_count):
            attr = attr_mod.create_attribute(
                owner_id=players[0].player_id,
                name=f"占位{i}",
                attr_type="utility",
                value=1,
                cap=1,
                narrative="",
                turn_no=1,
                max_cap=99,
            )
            players[0].attributes[attr.attr_id] = attr

        room.turn_no = 3
        asyncio.run(room._run_events(list(players)))

        self.assertEqual(len(players[0].attributes), room.pack.attributes.max_count)
        self.assertEqual(len(players[1].attributes), 1)

    def test_kill_records_death_without_inheritance(self) -> None:
        ev = make_event(trigger={"turns": [3]}, target="all", hp=-999)
        room, players, hub = make_room(pack_with(ev), n_players=2)
        room.turn_no = 3

        alive = asyncio.run(room._run_events(list(players)))

        self.assertEqual(alive, [])
        self.assertFalse(any(p.alive for p in players))
        types = [e.type for e in room.ledger]
        self.assertEqual(types.count(LedgerType.DEATH.value), 2)
        self.assertNotIn(LedgerType.INHERITANCE.value, types)
        death = next(e for e in room.ledger if e.type == LedgerType.DEATH.value)
        self.assertEqual(death.payload["cause"], "event")
        self.assertIsNone(death.payload["killer"])

    def test_hp_pct_scales_with_max_hp(self) -> None:
        ev = make_event(trigger={"turns": [3]}, target="all", hp=0, hp_pct=10)
        room, players, _ = make_room(pack_with(ev), n_players=2)
        players[0].hp_max, players[0].hp = 200, 100
        players[1].hp_max, players[1].hp = 50, 50
        room.turn_no = 3

        asyncio.run(room._run_events(list(players)))

        self.assertEqual(players[0].hp, 120)
        self.assertEqual(players[1].hp, 50)  # 已满血，不会被抬高


def judged_event(**overrides) -> EventCfg:
    data = {
        "id": "whisper",
        "name": "低语",
        "narrative": "有什么东西在 {target} 耳边说了一句话。",
        "trigger": {"turns": [3]},
        "target": "random",
        "mode": "judged",
        "judge_prompt": "勾起目标最不愿回想的事，造成 3~6 点精神伤害。",
    }
    data.update(overrides)
    return EventCfg.model_validate(data)


def judged_result(**overrides) -> "judge_ai.JudgeResult":
    verdict = judge.Verdict(
        effects=overrides.pop("effects", [{"player_id": "p0", "hp_delta": -7}]),
        attr_changes=overrides.pop("attr_changes", []),
        narrative=overrides.pop("narrative", "低语钻进甲的耳朵，他的呼吸乱了。"),
    )
    return judge_ai.JudgeResult(verdict=verdict, source=overrides.pop("source", "ai"), **overrides)


class JudgedEventTest(unittest.TestCase):
    def test_ai_verdict_is_applied(self) -> None:
        changes = [
            {
                "owner": "p0",
                "name": "心神不宁",
                "created": True,
                "attr_type": "utility",
                "value": 2,
                "cap": 4,
                "duration": 2,
                "narrative": "耳边一直有人在说话。",
            }
        ]
        result = judged_result(attr_changes=changes)
        captured: dict = {}

        async def fake(**kwargs):
            captured.update(kwargs)
            return result

        room, players, hub = make_room(pack_with(judged_event()), n_players=2)
        room.rng = StubRng()
        room.turn_no = 3

        with patch("server.game.judge_ai.adjudicate_event", new=fake):
            alive = asyncio.run(room._run_events(list(players)))

        self.assertEqual(len(alive), 2)
        self.assertEqual(players[0].hp, 93)
        self.assertEqual(len(players[0].attributes), 1)
        self.assertEqual(next(iter(players[0].attributes.values())).name, "心神不宁")
        self.assertEqual(captured["event_name"], "低语")
        self.assertIn("精神伤害", captured["event_prompt"])

        entry = next(e for e in room.ledger if e.type == LedgerType.EVENT.value)
        self.assertEqual(entry.payload["source"], "ai")
        self.assertIn("低语钻进甲的耳朵", entry.content)
        self.assertIn("甲 -7 → 93/100", entry.content)
        msg = next(m for m in hub.messages if m.get("type") == "special_event")
        self.assertEqual(msg["source"], "ai")
        self.assertEqual(msg["effects"][0]["delta"], -7)

    def test_ai_unavailable_falls_back_to_fixed(self) -> None:
        event = judged_event(hp=-6)
        result = judged_result(
            effects=[], attr_changes=[], narrative="", source="stub", reason="裁判代理未连接"
        )

        async def fake(**kwargs):
            return result

        room, players, _ = make_room(pack_with(event), n_players=2)
        room.rng = StubRng()
        room.turn_no = 3

        with patch("server.game.judge_ai.adjudicate_event", new=fake):
            asyncio.run(room._run_events(list(players)))

        self.assertEqual(players[0].hp, 94)
        entry = next(e for e in room.ledger if e.type == LedgerType.EVENT.value)
        self.assertNotEqual(entry.payload.get("source"), "ai")
        self.assertIn("耳边说了一句话", entry.content)

    def test_judged_kill_records_event_death(self) -> None:
        result = judged_result(
            effects=[{"player_id": "p0", "hp_delta": -999}],
            narrative="低语把甲拖进了自己的记忆里，再没出来。",
        )

        async def fake(**kwargs):
            return result

        room, players, _ = make_room(pack_with(judged_event()), n_players=2)
        room.rng = StubRng()
        room.turn_no = 3

        with patch("server.game.judge_ai.adjudicate_event", new=fake):
            alive = asyncio.run(room._run_events(list(players)))

        self.assertEqual([p.player_id for p in alive], ["p1"])
        self.assertFalse(players[0].alive)
        types = [e.type for e in room.ledger]
        self.assertEqual(types.count(LedgerType.DEATH.value), 1)
        self.assertNotIn(LedgerType.INHERITANCE.value, types)
        death = next(e for e in room.ledger if e.type == LedgerType.DEATH.value)
        self.assertEqual(death.payload["cause"], "event")
        self.assertIsNone(death.payload["killer"])


class EventPackValidationTest(unittest.TestCase):
    def _base_yaml(self, events_block: str) -> str:
        return f"""id: test-events
name: 事件测试包
hp:
  base: 100
  regen: 5
intensity:
  cap_base: 8
  cap_growth: 2
  ceiling_factor: 1.5
  level_words:
    寻常: 0.6
attributes:
  max_count: 10
  max_initial: 10
  max_cap: 30
  max_exchange_rate: 2.0
timers:
  declare_s: 60
  judge_s: 60
disaster:
  start_turn: 15
  every_n_turns: 3
  base_damage: 2
  growth: 1
appeal:
  count: 2
  window_s: 30
  vote_s: 60
inheritance:
  power_ratio: 0.4
events:
{events_block}
"""

    def test_missing_trigger_reports_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(
                self._base_yaml(
                    "  - id: no-trigger\n"
                    "    name: 无触发\n"
                    "    trigger:\n"
                    "      start_turn: 3\n"
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"YAP_PACKS_DIR": tmp}):
                registry = PackRegistry().reload()
        self.assertEqual(len(registry.issues), 1)
        message = registry.issues[0]["message"]
        self.assertIn("事件触发条件", message)
        self.assertIn("turns", message)

    def test_duplicate_event_ids_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.yaml"
            path.write_text(
                self._base_yaml(
                    "  - id: same\n"
                    "    name: 甲\n"
                    "    trigger:\n"
                    "      turns: [3]\n"
                    "  - id: same\n"
                    "    name: 乙\n"
                    "    trigger:\n"
                    "      turns: [4]\n"
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"YAP_PACKS_DIR": tmp}):
                registry = PackRegistry().reload()
        self.assertEqual(len(registry.issues), 1)
        self.assertIn("事件 id 重复", registry.issues[0]["message"])

    def test_builtin_packs_have_events(self) -> None:
        registry = PackRegistry().reload()
        self.assertEqual(registry.issues, [])
        for pack in registry.packs.values():
            self.assertGreaterEqual(len(pack.events), 2, pack.id)
            self.assertTrue(pack.public_view()["events"])


if __name__ == "__main__":
    unittest.main()
