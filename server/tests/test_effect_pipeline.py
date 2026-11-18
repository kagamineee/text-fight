"""受控模型输出验证两阶段提取/审核与引擎，不访问真实模型。"""
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from server.game import judge_ai
from server.game.effect_plan import DeclarationPlan, Requirement
from server.game.models import Declaration
from server.tests.test_effect_runtime import effect, plan
from server.tests.test_events import make_room, pack_with


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.room, self.players, self.hub = make_room(pack_with())
        self.actor, self.b, self.c = self.players
        env = patch.dict(os.environ, {"YAP_DEBUG_LOG": "0", "YAP_JUDGE_AI": "1"})
        env.start()
        self.addCleanup(env.stop)

    def link(self, text):
        return plan(effect([self.b.player_id, self.c.player_id], "restrict_targets", name="铁索连环",
                    trigger="passive", unit="global_turn", count=10,
                    allowed=[self.b.player_id, self.c.player_id]), quote=text)

    async def adjudicate(self, text, replies):
        with patch.object(judge_ai, "health", new=AsyncMock(return_value={"ok": True})), \
             patch.object(judge_ai, "chat_once", new=AsyncMock(side_effect=[json.dumps(r, ensure_ascii=False) for r in replies])) as chat:
            result = await judge_ai.adjudicate(players=self.room.players, turn_no=25, pack=self.room.pack,
                decl=Declaration(self.actor.player_id, text), ledger=[], active_effects=[])
            return result, chat

    async def test_timed_binding_preserved_and_review_is_separate(self):
        text = "将乙和丙捆绑，只能攻击彼此，持续10回合。"
        expected = self.link(text)
        result, chat = await self.adjudicate(text, [expected.model_dump(), {"approved": True, "issues": []}])
        self.assertEqual(result.plan, expected)
        self.assertEqual(chat.await_count, 2)
        review_call = chat.call_args_list[1].kwargs["messages"]
        self.assertEqual(len(review_call), 2)
        self.assertNotIn("推理", review_call[1]["content"])

    async def test_review_rejects_end_condition_substitution_and_retries(self):
        text = "只能攻击彼此，持续10回合。"
        wrong = self.link(text).model_dump()
        spec = wrong["requirements"][0]["effects"][0]
        spec["lifetime"] = {"unit": "permanent"}
        spec["end_condition"] = {"players": [self.b.player_id, self.c.player_id], "hp_below_pct": 1}
        right = self.link(text)
        result, chat = await self.adjudicate(text, [wrong,
            {"approved": False, "issues": ["把10回合换成了1%血量"]},
            right.model_dump(), {"approved": True, "issues": []}])
        self.assertEqual(result.plan, right)
        self.assertEqual(result.attempts, 2)
        self.assertIn("1%", chat.call_args_list[2].kwargs["messages"][-1]["content"])

    async def test_old_duel_output_cannot_bypass_pipeline(self):
        old = {"narrative": "绑定成功", "duels": [{"participants": [self.b.player_id, self.c.player_id], "until_hp_pct": 1}]}
        result, _ = await self.adjudicate("持续10回合", [old] * 3)
        self.assertEqual(result.source, "rejected")
        self.assertIsNone(result.plan)
        self.assertFalse(result.verdict.effects)
        self.assertIn("未执行", result.verdict.narrative)

    async def test_all_semantic_failures_do_not_use_approximate_stub(self):
        text = "攻击乙，持续10回合。"
        candidate = plan(effect([self.b.player_id], "damage", amount=10), quote=text).model_dump()
        result, _ = await self.adjudicate(text, [candidate, {"approved": False, "issues": ["遗漏持续时间"]}] * 3)
        self.assertEqual(result.source, "rejected")
        self.assertFalse(result.verdict.effects)

    async def test_unknown_field_rejected_instead_of_ignored(self):
        candidate = self.link("持续10回合").model_dump()
        candidate["requirements"][0]["effects"][0]["execute_when_broken"] = True
        result, chat = await self.adjudicate("持续10回合", [candidate] * 3)
        self.assertEqual(result.source, "rejected")
        self.assertEqual(chat.await_count, 3)

    async def test_missing_source_coverage_is_not_sent_to_review(self):
        candidate = self.link("只能攻击彼此").model_dump()
        result, chat = await self.adjudicate("只能攻击彼此，持续10回合", [candidate] * 3)
        self.assertEqual(result.source, "rejected")
        self.assertEqual(chat.await_count, 3)

    async def test_unsupported_reports_no_mechanism_created(self):
        text = "逆转时间回到开局"
        candidate = DeclarationPlan(requirements=[Requirement(quote=text, status="unsupported", reason="不支持时间回滚")])
        result, _ = await self.adjudicate(text, [candidate.model_dump(), {"approved": True, "issues": []}])
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=result)), \
             patch.object(self.room, "_schedule_summary"):
            await self.room._adjudicate(self.actor, Declaration(self.actor.player_id, text))
        self.assertFalse(self.room.effects.active)
        entry = next(m for m in self.hub.messages if m["type"] == "adjudication")
        self.assertIn("未执行", entry["narrative"])

    async def test_real_room_snapshot_uses_verified_effect_name_and_lifetime(self):
        text = "铁索连环持续10回合"
        result, _ = await self.adjudicate(text, [self.link(text).model_dump(), {"approved": True, "issues": []}])
        self.room.turn_no = 25
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=result)), \
             patch.object(self.room, "_schedule_summary"):
            await self.room._adjudicate(self.actor, Declaration(self.actor.player_id, text))
        snapshot = self.room.snapshot_for(self.actor.player_id)["state"]
        self.assertEqual(snapshot["active_effects"][0]["name"], "铁索连环")
        self.assertEqual(snapshot["active_effects"][0]["remaining"], 10)
        self.assertNotIn("duels", snapshot)
        self.assertNotIn("决斗", snapshot["active_effects"][0]["description"])

    async def test_offline_does_not_guess_complex_skill(self):
        with patch.dict(os.environ, {"YAP_JUDGE_AI": "0"}):
            result = await judge_ai.adjudicate(players=self.room.players, turn_no=25, pack=self.room.pack,
                decl=Declaration(self.actor.player_id, "释放铁索连环，持续10回合"), ledger=[])
        self.assertEqual(result.plan.requirements[0].status, "unsupported")

    async def test_legacy_damage_cannot_bypass_generic_shield(self):
        self.room.effects.apply(plan(effect([self.b.player_id], "shield", trigger="passive",
            unit="global_turn", count=10)), self.b.player_id, 1)
        result = judge_ai.JudgeResult(judge_ai.judge.Verdict(narrative="命中", effects=[
            {"player_id": self.b.player_id, "hp_delta": -10}]), "stub")
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=result)), \
             patch.object(self.room, "_schedule_summary"):
            await self.room._adjudicate(self.actor, Declaration(self.actor.player_id, "攻击"))
        self.assertEqual(self.b.hp, self.b.hp_max)

    async def test_rejected_declaration_cannot_trigger_legacy_keyword_rules(self):
        text = "无论何种情况都不允许解除，让乙与丙决斗，直到低于50%"
        rejected = judge_ai.JudgeResult(judge_ai.judge.Verdict(narrative="未执行"),
                                       "rejected", reason="审核拒绝，未执行")
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=rejected)), \
             patch.object(self.room, "_schedule_summary"):
            await self.room._adjudicate(self.actor, Declaration(self.actor.player_id, text))
        self.assertFalse(self.room.effects.active)
        self.assertTrue(all(not p.attributes for p in self.players))
        self.assertIn("未执行", self.room.ledger[-1].content)

    async def test_frozen_opportunity_decrements_actual_room_lifetime(self):
        self.room.effects.apply(plan(effect([self.actor.player_id], "skip_action", trigger="passive",
            unit="owner_turn", count=1, clock=self.actor.player_id)), self.b.player_id, 0)
        with patch.object(self.room, "_next_actor", return_value=self.actor):
            await self.room._play_turn(self.players)
        self.assertEqual(self.room.turn_no, 1)
        self.assertFalse(self.room.effects.active)
        self.assertTrue(any(e.payload.get("kind") == "effect_skip" for e in self.room.ledger))

    async def test_kill_and_drain_emit_death_once_and_snapshot_matches(self):
        self.actor.hp = 50
        self.b.hp = 5
        candidate = plan(effect([self.b.player_id], "drain", amount=10, beneficiary=self.actor.player_id))
        result = judge_ai.JudgeResult(judge_ai.judge.Verdict(narrative=""), "ai", plan=candidate)
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=result)), \
             patch.object(self.room, "_schedule_summary"), \
             patch.object(self.room, "_grant_inheritance", new=AsyncMock()) as inherit:
            await self.room._adjudicate(self.actor, Declaration(self.actor.player_id, "测试"))
        self.assertEqual(self.actor.hp, 55)
        self.assertEqual(len([m for m in self.hub.messages if m["type"] == "player_dead"]), 1)
        inherit.assert_awaited_once_with(self.b, self.actor.player_id)
        self.assertFalse(self.b.alive)

    async def test_extraction_timeout_is_rejected_without_damage(self):
        with patch.object(judge_ai, "health", new=AsyncMock(return_value={"ok": True})), \
             patch.object(judge_ai, "chat_once", new=AsyncMock(side_effect=TimeoutError)):
            result = await judge_ai.adjudicate(players=self.room.players, turn_no=1, pack=self.room.pack,
                decl=Declaration(self.actor.player_id, "攻击乙"), ledger=[], budget=0.05)
        self.assertEqual(result.source, "rejected")
        self.assertIsNone(result.plan)
        self.assertEqual(self.b.hp, 100)

    async def test_legacy_environment_crossing_threshold_cannot_be_undone_by_heal(self):
        from server.game.effect_plan import Condition
        spec = effect([self.b.player_id, self.c.player_id], "restrict_targets", trigger="passive",
            unit="permanent", allowed=[self.b.player_id, self.c.player_id])
        spec.end_condition = Condition(players=[self.b.player_id, self.c.player_id], hp_below_pct=50)
        self.room.effects.apply(plan(spec), self.actor.player_id, 1)
        self.b.hp = 50
        result = judge_ai.JudgeResult(judge_ai.judge.Verdict(narrative="环境", effects=[
            {"player_id": self.b.player_id, "hp_delta": -1},
            {"player_id": self.b.player_id, "hp_delta": 10}]), "ai")
        await self.room._apply_judged_event(result)
        self.assertEqual(self.b.hp, 59)
        self.assertFalse(self.room.effects.active)

    async def test_independent_review_catches_missing_protection(self):
        text = "让乙无法行动3回合，任何人都不能提前解除"
        candidate = plan(effect([self.b.player_id], "skip_action", trigger="passive",
            unit="global_turn", count=3), quote=text)
        corrected = candidate.model_copy(deep=True)
        corrected.requirements[0].effects[0].protected = True
        result, _ = await self.adjudicate(text, [candidate.model_dump(),
            {"approved": False, "issues": ["遗漏不可提前解除"]}, corrected.model_dump(),
            {"approved": True, "issues": []}])
        self.assertTrue(result.plan.requirements[0].effects[0].protected)
