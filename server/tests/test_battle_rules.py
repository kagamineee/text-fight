"""复现决斗契约被后手忽略的实战，以及禁锢/不可破坏的保护路径。"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from server.game import attributes, judge, judge_ai
from server.game.effect_plan import Condition, validate_plan
from server.game.offline_plan import parse_offline
from server.tests.test_effect_runtime import effect, plan
from server.game.models import Declaration, Phase
from server.tests.test_events import make_room, pack_with


class BattleRulesTest(unittest.TestCase):
    def setUp(self):
        self.room, self.players, self.hub = make_room(pack_with())
        self.host, self.a, self.b = self.players
        self.host.name = "神"
        self.a.name = "假人-1"
        self.b.name = "假人-2"
        self.room.turn_no = 4

    def adjudicate(self, actor, content, verdict, source="ai"):
        result = judge_ai.JudgeResult(verdict, source=source)
        with patch.object(judge_ai, "adjudicate", new=AsyncMock(return_value=result)):
            asyncio.run(self.room._adjudicate(actor, Declaration(actor.player_id, content)))

    def binding(self, participants=None):
        ids = participants or [self.a.player_id, self.b.player_id]
        spec = effect(ids, "restrict_targets", name="决斗契约", trigger="passive",
                      unit="permanent", allowed=ids)
        spec.end_condition = Condition(players=ids, hp_below_pct=50)
        return spec

    def seal(self):
        self.room.effects.apply(plan(self.binding()), self.host.player_id, 4)

    def test_recorded_duel_is_sealed_not_temporary_attr(self):
        self.seal()
        self.assertEqual(len(self.room.effects.active), 1)
        self.assertFalse(self.a.attributes)
        self.room.turn_no = 20
        self.room.effects.begin(20)
        self.assertEqual(len(self.room.effects.active), 1)
        self.assertEqual(self.room.snapshot_for(self.a.player_id)["state"]["active_effects"][0]["spec"]["end_condition"]["hp_below_pct"], 50)

    def test_recorded_turn_six_cannot_damage_host_even_when_model_allows(self):
        self.seal()
        self.room.turn_no = 6
        self.adjudicate(self.b, "重击神", judge.Verdict(
            effects=[{"player_id": self.host.player_id, "hp_delta": -18}],
            narrative="神被击中，损失18点血。"))
        self.assertEqual(self.host.hp, 100)
        self.assertFalse(self.host.attributes)
        self.assertIn("攻击目标限制", self.room.ledger[-1].content)
        self.assertNotIn("神被击中", self.room.ledger[-1].content)

    def test_fallback_and_human_are_also_restricted(self):
        self.seal()
        self.adjudicate(self.a, "攻击神", judge.Verdict(
            effects=[{"player_id": self.host.player_id, "hp_delta": -10}], narrative="打中了"), "stub")
        self.assertEqual(self.host.hp, 100)

    def test_opponent_damage_and_self_heal_remain_allowed(self):
        self.seal()
        self.a.hp = 80
        self.adjudicate(self.a, "攻击对手并治疗自己", judge.Verdict(effects=[
            {"player_id": self.b.player_id, "hp_delta": -10},
            {"player_id": self.a.player_id, "hp_delta": 5},
        ], narrative="击中对手"))
        self.assertEqual((self.a.hp, self.b.hp), (85, 90))

    def test_exact_half_remains_below_half_ends_and_does_not_reactivate(self):
        self.seal()
        self.b.hp = 50
        self.room.effects.begin(4)
        self.assertEqual(len(self.room.effects.active), 1)
        self.adjudicate(self.a, "攻击", judge.Verdict(effects=[
            {"player_id": self.b.player_id, "hp_delta": -1},
            {"player_id": self.b.player_id, "hp_delta": 10},
        ], narrative="攻击后治疗"))
        self.assertEqual(self.b.hp, 59)
        self.assertFalse(self.room.effects.active)

    def test_new_constraint_cannot_silently_replace_existing_opponent(self):
        self.seal()
        self.room.effects.apply(plan(self.binding([self.host.player_id, self.b.player_id])), self.host.player_id, 4)
        self.assertEqual(len(self.room.effects.active), 2)
        self.assertFalse(self.room.effects.allowed_targets(self.b.player_id))

    def test_dead_participant_ends_duel(self):
        self.seal()
        self.b.alive = False
        self.room.effects.begin(4)
        self.assertFalse(self.room.effects.active)

    def test_invalid_and_already_finished_duels_rejected(self):
        for ids in [["ghost", self.a.player_id], [self.a.player_id] * 2]:
            with self.assertRaises(ValueError):
                validate_plan(plan(self.binding(ids)), "测试", self.room.players, set())
        with self.assertRaises(ValueError):
            Condition(players=[self.a.player_id], hp_below_pct=101)
        self.a.hp = 49
        self.seal()
        self.assertFalse(self.room.effects.active)

    def test_duel_does_not_release_prior_freeze(self):
        frozen = attributes.create_attribute(owner_id=self.a.player_id, name="空间禁锢",
            attr_type="utility", value=1, cap=1, narrative="", turn_no=1,
            max_cap=30, freeze=True, duration=3, indestructible=True)
        self.a.attributes[frozen.attr_id] = frozen
        self.seal()
        self.adjudicate(self.a, "挣脱并攻击", judge.Verdict(effects=[
            {"player_id": self.b.player_id, "hp_delta": -20}], narrative="攻击成功"))
        self.assertEqual(self.b.hp, 100)
        self.assertEqual(frozen.charges, 3)

    def test_explicit_protection_is_executed_without_keyword_patching(self):
        spec = effect([self.a.player_id], "skip_action", trigger="passive", unit="global_turn", count=3)
        spec.protected = True
        self.room.effects.apply(plan(spec), self.host.player_id, 4)
        key = next(iter(self.room.effects.active))
        self.room.effects.apply(plan(effect([self.a.player_id], "remove_effect", effect_id=key)), self.b.player_id, 5)
        self.assertTrue(self.room.effects.frozen(self.a.player_id))
        self.assertIn(key, self.room.effects.active)

    def test_protected_shield_cannot_be_bypassed_by_pierce_flag(self):
        shield = attributes.create_attribute(owner_id=self.host.player_id, name="不可破坏屏障",
            attr_type="defense", value=1, cap=1, narrative="", turn_no=1, max_cap=30,
            shield=True, indestructible=True)
        self.host.attributes[shield.attr_id] = shield
        self.adjudicate(self.b, "无视一切防御", judge.Verdict(effects=[
            {"player_id": self.host.player_id, "hp_delta": -20, "pierce": True}], narrative="穿透成功"))
        self.assertEqual(self.host.hp, 100)
        self.assertNotIn("穿透成功", self.room.ledger[-1].content)

    def test_steal_cannot_bypass_protection_by_creating_copy_first(self):
        shield = attributes.create_attribute(owner_id=self.host.player_id, name="不灭壁垒",
            attr_type="defense", value=5, cap=5, narrative="", turn_no=1, max_cap=30,
            shield=True, indestructible=True)
        self.host.attributes[shield.attr_id] = shield
        asyncio.run(self.room._apply_attr_changes([
            {"owner": self.b.player_id, "name": shield.name, "created": True},
            {"owner": self.host.player_id, "attr_id": shield.attr_id, "remove": True},
            {"owner": self.host.player_id, "attr_id": shield.attr_id, "value": 0},
        ], self.b))
        self.assertEqual(shield.value, 5)
        self.assertIn(shield.attr_id, self.host.attributes)
        self.assertFalse(self.b.attributes)

    def test_ai_bot_receives_duel_target_even_if_host_has_lowest_hp(self):
        self.seal()
        self.host.hp = 1
        chat = AsyncMock(return_value="我攻击指定的决斗对手。")
        with patch.dict("os.environ", {"YAP_BOT_AI": "1"}):
            with patch.object(judge_ai, "health", new=AsyncMock(return_value={"ok": True})):
                with patch.object(judge_ai, "chat_once", new=chat):
                    asyncio.run(judge_ai.draft_bot_action(bot=self.b,
                        players=self.room.players, turn_no=6, pack=self.room.pack,
                        ledger=[], effect_context=self.room.effects.context(),
                        allowed_targets=self.room.effects.allowed_targets(self.b.player_id)))
        prompt = chat.call_args.kwargs["messages"][1]["content"]
        self.assertIn("建议优先针对：假人-1", prompt)
        self.assertIn("治疗/辅助不受限", prompt)

    def test_context_preserves_rules_outside_recent_ledger(self):
        self.seal()
        messages = judge_ai.build_messages(players=self.room.players, turn_no=30,
            pack=self.room.pack, decl=Declaration(self.b.player_id, "出手"), ledger=[],
            world_summary="", effect_context=self.room.effects.context())
        self.assertIn("无固定时限", messages[1]["content"])
        self.assertIn("低于50%", messages[1]["content"])

    def test_legacy_duel_output_cannot_create_a_generic_rule(self):
        from server.game.effect_plan import DeclarationPlan
        with self.assertRaises(ValueError):
            DeclarationPlan.model_validate({"narrative": "决斗成立", "duels": []})

    def test_reset_clears_rules(self):
        self.seal()
        asyncio.run(self.room.reset_for_lobby())
        self.assertFalse(self.room.effects.active)

    def test_template_bot_only_sees_duel_opponent(self):
        bot = self.room.add_player("假人-3", kind="bot", pid="bot", difficulty="normal")
        self.room.effects.apply(plan(self.binding([bot.player_id, self.a.player_id])), self.host.player_id, 4)
        self.room.phase = Phase.DECLARE
        self.room.current_actor = bot.player_id
        brain = self.room.bot_brains[bot.player_id]
        with patch("server.game.engine.BOT_THINK_MIN", 0), patch("server.game.engine.BOT_THINK_MAX", 0):
            with patch.object(brain, "declare", return_value=Declaration(bot.player_id, "攻击")) as draft:
                asyncio.run(self.room._bot_declare(bot, brain))
        self.assertEqual(set(draft.call_args.args[1]), {bot.player_id, self.a.player_id})

    def test_cancel_duel_is_not_new_creation(self):
        parsed = parse_offline("取消决斗：让假人1与假人2停止决斗，直到一方血量低于50%", self.host.player_id, self.room.players)
        self.assertEqual(parsed.requirements[0].status, "unsupported")


if __name__ == "__main__":
    unittest.main()
