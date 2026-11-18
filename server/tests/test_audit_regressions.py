"""全面审计发现的结算边界与裁判一致性回归。"""

import os
import unittest
from unittest.mock import AsyncMock, patch

from server.game import attributes, judge, judge_ai
from server.game.offline_plan import parse_offline
from server.game.formulas import clamp_new_attr
from server.game.models import Declaration, Phase
from server.packs.loader import validate_pack_text
from server.tests.test_events import make_room, pack_with, make_event
from server.tests.test_packs import pack_yaml


class AuditTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"YAP_JUDGE_AI": "0", "YAP_BOT_AI": "0", "YAP_DEBUG_LOG": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.room, self.players, self.hub = make_room(pack_with())
        self.room.turn_no = 1
        self.actor, self.target, self.third = self.players

    def attr(self, player, name="能力", **kwargs):
        attr = attributes.create_attribute(owner_id=player.player_id, name=name,
            attr_type="utility", value=1, cap=5, narrative="", turn_no=1, max_cap=30, **kwargs)
        player.attributes[attr.attr_id] = attr
        return attr

    async def test_death_stops_remaining_periodic_effects(self):
        self.actor.hp = 3
        self.attr(self.actor, "毒", per_turn=-5)
        self.attr(self.actor, "再生", per_turn=10)
        self.attr(self.actor, "毒二", per_turn=-15)
        alive = await self.room._run_per_turn(self.players)
        self.assertEqual(self.actor.hp, 0)
        self.assertFalse(self.actor.alive)
        self.assertNotIn(self.actor, alive)
        deaths = [m for m in self.hub.messages if m.get("type") == "player_dead"]
        self.assertEqual(len(deaths), 1)

    async def test_expired_effect_removed_before_environment(self):
        attr = self.attr(self.actor, duration=1, per_turn=-5)
        self.room._collector.wait = AsyncMock(return_value={})
        self.room._adjudicate = AsyncMock()
        async def disaster(alive):
            self.assertNotIn(attr.attr_id, self.actor.attributes)
            return alive
        self.room._run_disaster = disaster
        await self.room._play_turn(self.players)
        self.assertEqual(self.actor.hp, self.actor.hp_max)

    async def test_one_survivor_stops_before_next_environment_phase(self):
        for phase in ("_run_disaster", "_run_events", "_run_per_turn"):
            with self.subTest(phase=phase):
                room, players, _ = make_room(pack_with())
                mocks = {}
                for name in ("_run_disaster", "_run_events", "_run_per_turn"):
                    mocks[name] = AsyncMock(return_value=[players[0]] if name == phase else players)
                    setattr(room, name, mocks[name])
                room._adjudicate = AsyncMock()
                await room._play_turn(players)
                self.assertTrue(room.over)
                self.assertEqual(room.winner, players[0].player_id)
                room._adjudicate.assert_not_awaited()
                after = False
                for name, mock in mocks.items():
                    if after:
                        mock.assert_not_awaited()
                    after |= name == phase

    async def test_later_event_cannot_kill_sole_survivor(self):
        first = make_event(trigger={"turns": [1]}, hp=-5, target="all")
        second = make_event(id="second", trigger={"turns": [1]}, hp=-99, target="all")
        room, players, _ = make_room(pack_with(first, second))
        players[0].hp = 20
        players[1].hp = players[2].hp = 1
        await room._play_turn(players)
        self.assertEqual(room.winner, players[0].player_id)
        self.assertEqual(players[0].hp, 15)

    async def test_loop_error_finishes_and_can_reset(self):
        self.room._play_turn = AsyncMock(side_effect=RuntimeError("test fault"))
        with self.assertLogs("yap.engine", level="ERROR"):
            await self.room._loop()
        self.assertTrue(self.room.over)
        self.assertEqual(self.room.phase, Phase.OVER)
        self.assertIsNone(self.room.winner)
        self.assertIn("异常", self.room.result_view()["reason"])
        self.assertTrue(any(m.get("type") == "game_over" for m in self.hub.messages))
        await self.room.reset_for_lobby()
        self.assertFalse(self.room.over)
        self.assertEqual(self.room.end_reason, "")

    async def test_fallback_regeneration_is_positive(self):
        self.actor.hp = 50
        verdict = judge.adjudicate(self.room.players, 1, self.room.pack,
            Declaration(self.actor.player_id, "我凝聚「再生」，每回合恢复5点血"))
        self.assertEqual(verdict.attr_changes[0]["per_turn"], 5)
        await self.room._apply_attr_changes(verdict.attr_changes, self.actor)
        await self.room._run_per_turn(self.players)
        self.assertEqual(self.actor.hp, 55)

    async def test_fallback_cannot_bypass_shield_by_saying_ignore(self):
        self.attr(self.target, shield=True)
        await self.room._adjudicate(self.actor,
            Declaration(self.actor.player_id, f"无视防御攻击{self.target.name}，造成10点伤害"))
        self.assertEqual(self.target.hp, self.target.hp_max)

    def test_fallback_total_damage_and_heal_budget(self):
        for content, hp in (("攻击全场，造成12点伤害", 100), ("治疗全场，恢复12点血", 50)):
            for player in self.players:
                player.hp = hp
            verdict = judge.adjudicate(self.room.players, 1, self.room.pack,
                Declaration(self.actor.player_id, content))
            self.assertTrue(verdict.effects)
            self.assertLessEqual(sum(abs(e["hp_delta"]) for e in verdict.effects), 12)

    async def test_shield_consumes_and_heals_with_raw_damage(self):
        shield = self.attr(self.target, shield=True, shield_charges=2, block_heal_pct=10)
        self.actor.hp = 50
        out = judge_ai.JudgeOut(narrative="攻击被盾挡下", effects=[
            judge_ai.EffectOut(player_id=self.target.player_id, hp_delta=-8)])
        verdict, clamped = judge_ai.materialize(out, self.room.players, self.room.pack, 1)
        result = judge_ai.JudgeResult(verdict, "ai", clamped=clamped)
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=result)):
            await self.room._adjudicate(self.actor, Declaration(self.actor.player_id, "攻击"))
        self.assertEqual(shield.shield_charges, 1)
        self.assertEqual(self.actor.hp, 60)
        self.assertEqual(self.target.hp, 100)

    def test_lethal_only_context_for_unlimited_shield(self):
        self.attr(self.target, shield=True, lethal_only=True)
        context = "\n".join(judge_ai._attr_lines(self.room.players, 1))
        self.assertIn("非致命攻击正常扣血", context)

    async def test_existing_attribute_renewal_and_effect_updates(self):
        attr = self.attr(self.actor, duration=2, shield=True)
        self.room.turn_no = 2
        out = judge_ai.JudgeOut(narrative="续期并恢复", attr_changes=[
            judge_ai.AttrChangeOut(owner=self.actor.player_id, attr_id=attr.attr_id,
                duration=4, per_turn=3, shield_charges=2, damage_reduction=25,
                cap=12, narrative="新效果")])
        verdict, _ = judge_ai.materialize(out, self.room.players, self.room.pack, 2)
        await self.room._apply_attr_changes(verdict.attr_changes, self.actor)
        self.assertEqual(attr.expires_turn, 5)
        self.assertEqual(attr.per_turn, 3)
        self.assertEqual(attr.shield_charges, 2)
        self.assertEqual(attr.damage_reduction, 25)
        self.assertEqual(attr.cap, 12)
        self.assertEqual(attr.narrative, "新效果")
        self.assertTrue(attr.shield)
        self.assertEqual(len(self.actor.attributes), 1)

    async def test_same_name_update_and_explicit_false_are_preserved(self):
        attr = self.attr(self.actor, shield=True, per_turn=2)
        out = judge_ai.JudgeOut(narrative="改变", attr_changes=[judge_ai.AttrChangeOut(
            owner=self.actor.player_id, name=attr.name, shield=False, per_turn=0)])
        verdict, _ = judge_ai.materialize(out, self.room.players, self.room.pack, 1)
        await self.room._apply_attr_changes(verdict.attr_changes, self.actor)
        self.assertFalse(attr.shield)
        self.assertEqual(attr.per_turn, 0)
        self.assertEqual(attr.value, 1)

    async def test_freeze_renewal_and_protected_update(self):
        attr = self.attr(self.target, freeze=True, duration=2)
        out = judge_ai.JudgeOut(narrative="延长", attr_changes=[judge_ai.AttrChangeOut(
            owner=self.target.player_id, attr_id=attr.attr_id, duration=4)])
        verdict, _ = judge_ai.materialize(out, self.room.players, self.room.pack, 1)
        await self.room._apply_attr_changes(verdict.attr_changes, self.actor)
        self.assertEqual(attr.charges, 4)
        self.assertEqual(attr.expires_turn, 0)
        attr.indestructible = True
        await self.room._apply_attr_changes([{"owner": self.target.player_id,
            "attr_id": attr.attr_id, "freeze": False, "duration": 0}], self.actor)
        self.assertTrue(attr.freeze)
        self.assertEqual(attr.charges, 4)

    def test_negation_and_ambiguous_duel_are_not_forced(self):
        for prefix in ("我不会让", "不要让", "我没有让", "如果让", "是否让", "拒绝让"):
            parsed = parse_offline(f"{prefix}乙与丙进行决斗，直到一方血量低于50%",
                                   self.actor.player_id, self.room.players)
            self.assertEqual(parsed.requirements[0].status, "unsupported")

    async def test_ai_off_disables_summary_call(self):
        self.room.turn_no = 10
        with patch.object(judge_ai, "chat_once", new=AsyncMock()) as chat:
            self.room._schedule_summary()
            self.assertIsNone(self.room._summary_task)
            chat.assert_not_awaited()

    def test_invalid_caps_rejected_and_clamp_is_defensive(self):
        text = pack_yaml().replace("max_initial: 10", "max_initial: 100").replace("max_cap: 30", "max_cap: 10")
        pack, errors = validate_pack_text(text)
        self.assertIsNone(pack)
        self.assertTrue(errors)
        self.assertEqual(clamp_new_attr(100, 100, 100, 10), (10, 10))

    async def test_tie_break_applies_in_order_and_preserves_exact_ties(self):
        self.room.pack = self.room.pack.model_copy(update={"tie_break": ["hp_peak", "survived_turns"]})
        for player in self.players:
            player.alive = False
            player.hp = 0
        self.actor.hp_peak = 150
        self.target.survived_turns = 20
        await self.room._finish([])
        self.assertEqual(self.room.winner, self.actor.player_id)
        self.actor.hp_peak = self.target.hp_peak
        self.actor.survived_turns = self.target.survived_turns
        await self.room._finish([])
        self.assertIsNone(self.room.winner)

    def test_invalid_ranking_keys_rejected(self):
        pack, errors = validate_pack_text(pack_yaml() + "\ntie_break: [not_a_player_field]\n")
        self.assertIsNone(pack)
        self.assertTrue(errors)
