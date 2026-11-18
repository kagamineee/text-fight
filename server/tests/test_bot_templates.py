"""模板发言必须可执行，并读取通用状态；真人文本不做模糊近似。"""
import asyncio
import os
import unittest
from unittest.mock import patch

from server.game import bots, judge_ai
from server.game.effect_plan import Action, EffectSpec, Lifetime, validate_plan
from server.game.models import Declaration
from server.game.offline_plan import parse_offline
from server.tests.test_p2 import make_room
from server.tests.test_inheritance_effects import plan, positive


class BotTemplateTest(unittest.TestCase):
    def setUp(self):
        self.room, self.players, _ = make_room(3)
        self.a, self.b, self.c = self.players

    def parse(self, text):
        return parse_offline(text, self.a.player_id, self.room.players, self.room.effects.view())

    def apply(self, text, turn=2):
        parsed = self.parse(text)
        self.assertTrue(all(r.status == "supported" for r in parsed.requirements), text)
        ids = set(self.room.effects.active) | {a.attr_id for p in self.players for a in p.attributes.values()}
        validate_plan(parsed, text, self.room.players, ids)
        return self.room.effects.apply(parsed, self.a.player_id, turn)

    def test_timed_armor_freeze_and_upgrade_actually_execute(self):
        text = bots.CREATE_ARMOR.format(name="任意名称", value=5, turns=7)
        self.apply(text)
        key = next(iter(self.room.effects.active))
        self.room.effects.end_turn(3, self.b.player_id)
        text = bots.UPGRADE_ARMOR.format(attr="任意名称", effect_id=key, value=7)
        self.apply(text, 4)
        effect = self.room.effects.active[key]
        self.assertEqual(effect.remaining, 6)
        self.assertEqual(effect.spec.actions[0].amount, 7)
        text = bots.FREEZE_ATTR.format(target=self.b.name, turns=2)
        self.apply(text, 5)
        self.assertTrue(self.room.effects.frozen(self.b.player_id))
        self.room.effects.end_turn(6, self.b.player_id)
        self.room.effects.end_turn(7, self.c.player_id)
        self.assertFalse(self.room.effects.frozen(self.b.player_id))

    def test_disarm_targets_exact_effect_and_then_damages(self):
        shield = positive(self.b.player_id, "shield", charges=2)
        self.room.effects.apply(plan(shield), self.b.player_id, 1)
        key = next(iter(self.room.effects.active))
        self.apply(bots.DISPEL_ATTACK.format(target=self.b.name, attr=shield.name, effect_id=key, dmg=5))
        self.assertNotIn(key, self.room.effects.active)
        self.assertEqual(self.b.hp, 95)

    def test_protected_disarm_rejects_whole_requirement(self):
        shield = positive(self.b.player_id, "shield", charges=2)
        shield.protected = True
        self.room.effects.apply(plan(shield), self.b.player_id, 1)
        key = next(iter(self.room.effects.active))
        self.apply(bots.DISPEL_ATTACK.format(target=self.b.name, attr=shield.name, effect_id=key, dmg=5))
        self.assertIn(key, self.room.effects.active)
        self.assertEqual(self.b.hp, 100)

    def test_ambiguous_old_templates_and_extra_words_are_not_approximated(self):
        for text in ["我沉肩蓄力，凝聚「神盾」，强度 8 点。",
                     "我稳住气息，强化「剑」，把它提到 8 点。",
                     bots.CREATE_ARMOR.format(name="甲", value=8, turns=3) + "并且永远不死",
                     bots.CREATE_ARMOR.format(name="甲", value=8, turns=1001)]:
            self.assertEqual(self.parse(text).requirements[0].status, "unsupported", text)

    def test_ambiguous_player_name_and_wrong_effect_name_reject(self):
        self.c.name = self.b.name
        self.assertEqual(self.parse(bots.FREEZE_ATTR.format(target=self.b.name, turns=2)).requirements[0].status,
                         "unsupported")
        self.room.effects.apply(plan(positive(self.a.player_id, amount=3)), self.a.player_id, 1)
        key = next(iter(self.room.effects.active))
        self.assertEqual(self.parse(bots.UPGRADE_ARMOR.format(attr="错名", effect_id=key, value=7))
                         .requirements[0].status, "unsupported")

    def declare(self, difficulty, roll):
        brain = bots.BotBrain(5, difficulty)
        with patch.object(brain.rng, "random", return_value=roll):
            return brain.declare(self.a, self.room.players, 10, self.room.pack, effects=self.room.effects).content

    def test_bot_does_not_duplicate_generic_defense(self):
        self.room.effects.apply(plan(positive(self.a.player_id)), self.a.player_id, 1)
        text = self.declare("normal", 0.2)
        self.assertTrue(any(a.kind == "damage" for e in self.parse(text).requirements[0].effects for a in e.actions))

    def test_bot_does_not_refreeze_frozen_target(self):
        freeze = EffectSpec(name="冻结", targets=[self.b.player_id], trigger="passive",
            lifetime=Lifetime(unit="global_turn", count=3), actions=[Action(kind="skip_action")])
        self.room.effects.apply(plan(freeze), self.c.player_id, 1)
        text = self.declare("hard", 0.7)
        self.assertNotIn("冻住", text)

    def test_inferno_disarms_generic_shield_but_not_protected_or_negative_effect(self):
        spec = positive(self.b.player_id, "shield", charges=2)
        self.room.effects.apply(plan(spec), self.b.player_id, 1)
        self.assertIn("我解除", self.declare("inferno", 0.2))
        next(iter(self.room.effects.active.values())).spec.protected = True
        self.assertNotIn("我解除", self.declare("inferno", 0.2))
        next(iter(self.room.effects.active.values())).spec.protected = False
        next(iter(self.room.effects.active.values())).spec.actions.append(Action(kind="skip_action"))
        self.assertNotIn("我解除", self.declare("inferno", 0.2))

    def test_bot_upgrades_generic_armor_without_renewal(self):
        self.room.effects.apply(plan(positive(self.a.player_id, amount=2, count=3)), self.a.player_id, 1)
        text = self.declare("hard", 0.2)
        req = self.parse(text).requirements[0]
        self.assertEqual(len(req.revisions), 1)
        self.assertFalse(req.revisions[0].renew)

    def test_capacity_counts_new_effects_and_attack_constraints(self):
        for _ in range(self.room.pack.attributes.max_count):
            heal = positive(self.a.player_id, "heal", amount=1, trigger="global_start")
            self.room.effects.apply(plan(heal), self.a.player_id, 1)
        text = self.declare("normal", 0.2)
        self.assertNotIn("固定抵扣", text)
        restriction = EffectSpec(name="限制", targets=[self.b.player_id], trigger="passive",
            lifetime=Lifetime(unit="permanent"), actions=[Action(kind="restrict_targets", allowed=[self.c.player_id])])
        self.room.effects.apply(plan(restriction), self.c.player_id, 1)
        brain = bots.BotBrain(5, "normal")
        with patch.object(brain.rng, "random", return_value=0.9):
            decl = brain.declare(self.b, self.room.players, 10, self.room.pack, effects=self.room.effects)
        parsed = parse_offline(decl.content, self.b.player_id, self.room.players, self.room.effects.view())
        self.assertEqual(parsed.requirements[0].effects[0].targets, [self.c.player_id])

    def test_all_difficulties_only_emit_supported_templates_across_seeds(self):
        for difficulty in bots.DIFFICULTIES:
            for seed in range(60):
                brain = bots.BotBrain(seed, difficulty)
                self.a.hp = 20 if seed % 2 else 100
                text = brain.declare(self.a, self.room.players, 10, self.room.pack, effects=self.room.effects).content
                self.assertEqual(self.parse(text).requirements[0].status, "supported", (difficulty, seed, text))

    def test_fallback_judge_receives_effect_context_for_revision(self):
        self.room.effects.apply(plan(positive(self.a.player_id, amount=2)), self.a.player_id, 1)
        text = self.declare("hard", 0.2)
        with patch.dict(os.environ, {"YAP_JUDGE_AI": "0"}):
            result = asyncio.run(judge_ai.adjudicate(players=self.room.players, turn_no=2,
                pack=self.room.pack, decl=Declaration(self.a.player_id, text), ledger=[],
                active_effects=self.room.effects.view()))
        self.assertEqual(len(result.plan.requirements[0].revisions), 1)

    def test_duplicate_names_fall_back_to_unambiguous_self_action(self):
        self.c.name = self.b.name
        text = self.declare("inferno", 0.9)
        req = self.parse(text).requirements[0]
        self.assertEqual(req.status, "supported")
        self.assertEqual(req.effects[0].targets, [self.a.player_id])
        self.assertEqual(req.effects[0].actions[0].kind, "heal")
