"""可组合效果的性质测试：不依赖技能名称，不请求外部服务。"""
import copy
import unittest

from server.game.effect_plan import (Action, DeclarationPlan, EffectSpec, Lifetime,
                                    Requirement, Condition, Revision, validate_plan)
from server.game.effect_runtime import EffectRuntime
from server.tests.test_events import make_room, pack_with
from server.game import attributes


def effect(targets, kind, *, name="任意技能", trigger="immediate", unit="instant", count=None,
           clock=None, trigger_player=None, amount=None, **kwargs):
    return EffectSpec(name=name, targets=targets, trigger=trigger, trigger_player=trigger_player,
        lifetime=Lifetime(unit=unit, count=count, clock_player=clock),
        actions=[Action(kind=kind, amount=amount, **kwargs)])


def plan(*effects, quote="测试"):
    return DeclarationPlan(requirements=[Requirement(quote=quote, status="supported", effects=list(effects))])


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.room, self.players, _ = make_room(pack_with())
        self.a, self.b, self.c = [p.player_id for p in self.players]
        self.runtime = self.room.effects

    def link(self, name="铁索连环", unit="global_turn", count=10, clock=None):
        return effect([self.b, self.c], "restrict_targets", name=name, trigger="passive",
                      unit=unit, count=count, clock=clock, allowed=[self.b, self.c])

    def hit(self, source, target, amount=10, kind="damage", **kwargs):
        return self.runtime.apply(plan(effect([target], kind, amount=amount, **kwargs)), source, 30)

    def test_ten_turn_limit_does_not_become_blood_threshold(self):
        self.runtime.apply(plan(self.link()), self.a, 25)
        spec = next(iter(self.runtime.active.values())).spec
        self.assertIsNone(spec.end_condition)
        self.assertFalse(spec.protected)
        self.runtime.end_turn(25, self.a)
        for turn in range(26, 35):
            self.assertEqual(self.runtime.allowed_targets(self.b), {self.c})
            self.runtime.end_turn(turn, self.b)
        self.assertEqual(next(iter(self.runtime.active.values())).remaining, 1)
        self.runtime.end_turn(35, self.c)
        self.assertFalse(self.runtime.active)
        self.assertIn(self.a, self.runtime.allowed_targets(self.b))

    def test_skill_names_do_not_change_behavior(self):
        for name in ("铁索连环", "宿命契约", "冰糖葫芦", "任意新名字"):
            self.runtime.reset()
            self.runtime.apply(plan(self.link(name)), self.a, 25)
            self.assertEqual(self.runtime.allowed_targets(self.b), {self.c})
            self.assertEqual(next(iter(self.runtime.active.values())).remaining, 10)

    def test_attack_restriction_does_not_block_healing_third_party(self):
        self.runtime.apply(plan(self.link()), self.a, 25)
        self.players[0].hp = 40
        self.hit(self.b, self.a)
        self.assertEqual(self.players[0].hp, 40)
        self.hit(self.b, self.a, kind="heal")
        self.assertEqual(self.players[0].hp, 50)

    def test_hp_end_condition_is_independent_and_strict(self):
        spec = self.link(unit="permanent", count=None)
        spec.end_condition = Condition(players=[self.b, self.c], hp_below_pct=50)
        self.runtime.apply(plan(spec), self.a, 25)
        self.players[1].hp = 50
        self.runtime.begin(26)
        self.assertTrue(self.runtime.active)
        self.hit(self.c, self.b, amount=1)
        self.assertFalse(self.runtime.active)
        self.hit(self.a, self.b, amount=20, kind="heal")
        self.assertFalse(self.runtime.active)

    def test_owner_turn_counts_only_owner_opportunities(self):
        self.runtime.apply(plan(self.link(unit="owner_turn", count=2, clock=self.b)), self.a, 1)
        self.runtime.end_turn(2, self.a)
        self.runtime.end_turn(3, self.c)
        self.assertEqual(next(iter(self.runtime.active.values())).remaining, 2)
        self.runtime.end_turn(4, self.b)
        self.runtime.end_turn(5, self.b)
        self.assertFalse(self.runtime.active)

    def test_round_waits_for_each_living_player(self):
        self.runtime.apply(plan(self.link(unit="round", count=1)), self.a, 1)
        self.runtime.end_turn(2, self.b)
        self.runtime.end_turn(3, self.b)
        self.assertTrue(self.runtime.active)
        self.players[2].alive = False
        self.players[2].hp = 0
        self.runtime.end_turn(4, self.a)
        self.assertFalse(self.runtime.active)

    def test_periodic_lifesteal_uses_actual_loss_and_owner_trigger(self):
        spec = effect([self.b], "drain", amount=50, beneficiary=self.a, trigger="owner_start",
                      trigger_player=self.b, unit="owner_turn", count=2, clock=self.b)
        self.players[0].hp = 20
        self.players[1].hp = 7
        self.runtime.apply(plan(spec), self.a, 25)
        self.runtime.start_turn(26, self.c)
        self.assertEqual(self.players[0].hp, 20)
        self.runtime.start_turn(27, self.b)
        self.assertEqual(self.players[0].hp, 27)
        self.assertFalse(self.players[1].alive)
        self.assertEqual(self.players[1].hp, 0)

    def test_drain_obeys_shields_and_armor(self):
        armor = effect([self.b], "armor", amount=5, trigger="passive", unit="permanent")
        self.runtime.apply(plan(armor), self.b, 25)
        self.players[0].hp = 20
        self.hit(self.a, self.b, amount=10, kind="drain", beneficiary=self.a)
        self.assertEqual(self.players[0].hp, 25)
        shield = effect([self.b], "shield", trigger="passive", unit="global_turn", count=10)
        self.runtime.apply(plan(shield), self.b, 25)
        self.hit(self.a, self.b, amount=10, kind="drain", beneficiary=self.a, bypass_armor=True)
        self.assertEqual(self.players[0].hp, 25)

    def test_block_trigger_works_on_last_charge(self):
        shield = effect([self.b], "shield", trigger="passive", unit="global_turn", count=10, charges=1)
        heal = effect([self.a, self.b], "heal", amount=10, trigger="after_block", trigger_player=self.b,
                      unit="global_turn", count=10)
        self.players[0].hp = self.players[1].hp = 50
        self.runtime.apply(plan(shield, heal), self.b, 25)
        self.hit(self.a, self.b)
        self.assertEqual([p.hp for p in self.players[:2]], [60, 60])
        self.assertFalse(list(self.runtime.passives(self.b, "shield")))

    def test_lethal_shield_uses_projected_damage(self):
        self.players[1].hp = 15
        self.runtime.apply(plan(effect([self.b], "shield", trigger="passive", unit="permanent",
            charges=2, lethal_only=True)), self.b, 25)
        self.hit(self.a, self.b, amount=5)
        self.assertEqual(self.players[1].hp, 10)
        self.hit(self.a, self.b, amount=10)
        self.assertEqual(self.players[1].hp, 10)
        self.assertEqual(next(iter(self.runtime.active.values())).charges[f"{self.b}:0"], 1)

    def test_removed_trigger_is_not_expiration_trigger(self):
        self.runtime.apply(plan(self.link(count=1)), self.a, 25)
        key = next(iter(self.runtime.active))
        data = self.link().model_dump()
        data.update(trigger="removed", watch_effect=key, selection="lowest_hp",
                    actions=[{"kind": "damage", "amount": 10}])
        revenge = EffectSpec.model_validate(data)
        self.runtime.apply(plan(revenge), self.a, 25)
        self.runtime.end_turn(26, self.b)
        self.assertEqual(self.players[1].hp, 100)
        self.runtime.reset()
        self.runtime.apply(plan(self.link()), self.a, 25)
        key = next(iter(self.runtime.active))
        revenge.watch_effect = key
        self.runtime.apply(plan(revenge), self.a, 25)
        self.players[2].hp = 50
        self.runtime.apply(plan(effect([self.b], "remove_effect", effect_id=key)), self.a, 26)
        self.assertEqual(self.players[2].hp, 40)
        self.assertEqual(self.players[1].hp, 100)

    def test_trigger_chain_is_bounded(self):
        for source, target in ((self.a, self.b), (self.b, self.a)):
            self.runtime.apply(plan(effect([target], "damage", amount=1, trigger="after_damage",
                trigger_player=source, unit="permanent")), source, 25)
        records = self.hit(self.a, self.b, amount=1)
        self.assertLessEqual(len([r for r in records if r["kind"] == "hp"]), 3)
        self.assertFalse(self.runtime.queue)

    def test_no_healing_after_death(self):
        self.players[1].hp = 5
        kill = effect([self.b], "damage", amount=10)
        heal = effect([self.b], "heal", amount=10)
        self.runtime.apply(plan(kill, heal), self.a, 25)
        self.assertEqual(self.players[1].hp, 0)
        self.assertFalse(self.players[1].alive)

    def test_total_action_budget_shared_across_requirements(self):
        requirements = [Requirement(quote="测试", status="supported",
            effects=[effect([pid], "damage", amount=12)]) for pid in (self.b, self.c)]
        records = self.runtime.apply(DeclarationPlan(requirements=requirements), self.a, 1)
        self.assertEqual(sum(-r["hp_delta"] for r in records if r["kind"] == "hp"), 12)
        self.assertTrue(any(r["kind"] == "limited" for r in records))

    def test_replay_is_deterministic(self):
        runtime2 = EffectRuntime(copy.deepcopy(self.runtime.players), self.room.pack)
        for rt in (self.runtime, runtime2):
            rt.apply(plan(self.link()), self.a, 25)
            rt.apply(plan(effect([self.c], "damage", amount=9)), self.b, 26)
            rt.end_turn(26, self.b)
        self.assertEqual(self.runtime.view(), runtime2.view())
        self.assertEqual([p.hp for p in self.runtime.players.values()], [p.hp for p in runtime2.players.values()])

    def test_protection_prevents_remove_and_rewrite(self):
        spec = self.link()
        spec.protected = True
        self.runtime.apply(plan(spec), self.a, 25)
        key = next(iter(self.runtime.active))
        self.runtime.apply(plan(effect([self.b], "remove_effect", effect_id=key)), self.a, 26)
        replacement = self.link(count=1)
        req = Requirement(quote="改写", status="supported", revisions=[Revision(effect_id=key, replacement=replacement, renew=True)])
        self.runtime.apply(DeclarationPlan(requirements=[req]), self.b, 26)
        self.assertTrue(self.runtime.active[key].spec.protected)
        self.assertEqual(self.runtime.active[key].remaining, 10)

    def test_update_does_not_silently_renew(self):
        self.runtime.apply(plan(self.link()), self.a, 25)
        key = next(iter(self.runtime.active))
        self.runtime.end_turn(26, self.b)
        req = Requirement(quote="更名", status="supported", revisions=[Revision(effect_id=key, replacement=self.link("新名字"))])
        self.runtime.apply(DeclarationPlan(requirements=[req]), self.a, 27)
        self.assertEqual(self.runtime.active[key].remaining, 9)
        req.revisions[0].renew = True
        self.runtime.apply(DeclarationPlan(requirements=[req]), self.a, 28)
        self.assertEqual(self.runtime.active[key].remaining, 10)

    def test_legacy_shield_is_respected_by_new_damage(self):
        shield = attributes.create_attribute(owner_id=self.b, name="旧盾", attr_type="utility", value=1,
            cap=1, narrative="", turn_no=1, max_cap=10, shield=True)
        self.players[1].attributes[shield.attr_id] = shield
        self.hit(self.a, self.b)
        self.assertEqual(self.players[1].hp, 100)

    def test_unknown_fields_and_incomplete_coverage_rejected(self):
        raw = plan(self.link()).model_dump()
        raw["requirements"][0]["effects"][0]["duel"] = True
        with self.assertRaises(ValueError):
            DeclarationPlan.model_validate(raw)
        with self.assertRaises(ValueError):
            validate_plan(plan(self.link(), quote="限制"), "限制，持续10回合", self.room.players, set())

    def test_unsupported_requirement_cannot_hide_partial_execution(self):
        with self.assertRaises(ValueError):
            Requirement(quote="吸血", status="unsupported", reason="缺少关联治疗", effects=[effect([self.b], "damage", amount=5)])

    def test_generic_armor_respects_pack_and_reports_limit(self):
        records = self.runtime.apply(plan(effect([self.b], "armor", amount=999, trigger="passive", unit="permanent")), self.a, 25)
        self.assertTrue(any(r["kind"] == "limited" for r in records))
        self.assertEqual(next(iter(self.runtime.active.values())).spec.actions[0].amount, self.room.pack.attributes.max_initial)

    def test_periodic_effect_has_exact_number_of_ticks(self):
        self.players[1].hp = 50
        self.runtime.apply(plan(effect([self.b], "heal", amount=2, trigger="global_start",
            unit="global_turn", count=10)), self.a, 25)
        for turn in range(26, 36):
            self.runtime.start_turn(turn, self.b)
            self.runtime.end_turn(turn, self.b)
        self.runtime.start_turn(36, self.b)
        self.assertEqual(self.players[1].hp, 70)
        self.assertFalse(self.runtime.active)

    def test_multi_target_shields_consume_separate_charges(self):
        self.runtime.apply(plan(effect([self.b, self.c], "shield", charges=1,
            trigger="passive", unit="permanent")), self.a, 25)
        self.hit(self.a, self.b)
        self.assertTrue(list(self.runtime.passives(self.c, "shield")))
        self.assertFalse(list(self.runtime.passives(self.b, "shield")))
        self.hit(self.a, self.b)
        self.assertEqual(self.players[1].hp, 90)
        self.hit(self.a, self.c)
        self.assertFalse(self.runtime.active)

    def test_environment_damage_triggers_once_without_duplicate_damage(self):
        self.runtime.apply(plan(effect([self.b], "heal", amount=5, trigger="after_damage",
            trigger_player=self.b, unit="permanent")), self.a, 25)
        self.players[1].hp = 80
        records = self.runtime.observe_environment(26, [{"player_id": self.b, "delta": -20}])
        self.assertEqual(self.players[1].hp, 85)
        self.assertEqual(sum(r["hp_delta"] for r in records if r["kind"] == "hp"), 5)

    def test_finite_protected_effect_still_expires(self):
        spec = self.link(count=1)
        spec.protected = True
        self.runtime.apply(plan(spec), self.a, 25)
        self.runtime.end_turn(26, self.b)
        self.assertFalse(self.runtime.active)

    def test_freeze_blocks_complete_plan(self):
        self.runtime.apply(plan(effect([self.b], "skip_action", trigger="passive",
            unit="owner_turn", count=2, clock=self.b)), self.a, 25)
        records = self.hit(self.b, self.a)
        self.assertEqual(self.players[0].hp, 100)
        self.assertTrue(any(r["kind"] == "blocked" for r in records))

    def test_conflicting_target_constraints_intersect(self):
        self.runtime.apply(plan(self.link()), self.a, 25)
        spec = effect([self.b], "restrict_targets", trigger="passive", unit="global_turn", count=10,
                      allowed=[self.a])
        self.runtime.apply(plan(spec), self.a, 25)
        self.assertFalse(self.runtime.allowed_targets(self.b))

    def test_no_silent_shortening_of_large_duration(self):
        self.runtime.apply(plan(self.link(count=100)), self.a, 25)
        self.assertEqual(next(iter(self.runtime.active.values())).remaining, 100)
        with self.assertRaises(ValueError):
            Lifetime(unit="global_turn", count=1001)

    def test_local_chain_and_break_trigger_can_be_created_together(self):
        link = self.link()
        link.local_id = "local:chain"
        revenge = EffectSpec.model_validate({**self.link().model_dump(), "trigger": "removed",
            "watch_effect": "local:chain", "selection": "lowest_hp",
            "actions": [{"kind": "damage", "amount": 10}]})
        candidate = plan(link, revenge)
        validate_plan(candidate, "测试", self.room.players, set())
        self.runtime.apply(candidate, self.a, 25)
        key = next(iter(self.runtime.active))
        listener = list(self.runtime.active.values())[1]
        self.assertEqual(listener.spec.watch_effect, key)
        self.assertIsNone(self.runtime.active[key].spec.local_id)
        self.players[2].hp = 50
        self.runtime.apply(plan(effect([self.b], "remove_effect", effect_id=key)), self.a, 26)
        self.assertEqual(self.players[2].hp, 40)
        self.assertFalse(self.runtime.active)

    def test_forward_local_reference_is_rejected(self):
        link = self.link()
        link.local_id = "local:chain"
        listener = EffectSpec.model_validate({**link.model_dump(), "local_id": None,
            "trigger": "removed", "watch_effect": "local:chain",
            "actions": [{"kind": "heal", "amount": 10}]})
        with self.assertRaises(ValueError):
            validate_plan(plan(listener, link), "测试", self.room.players, set())

    def test_protected_removal_blocks_dependent_damage_in_same_requirement(self):
        link = self.link()
        link.protected = True
        self.runtime.apply(plan(link), self.a, 25)
        key = next(iter(self.runtime.active))
        records = self.runtime.apply(plan(effect([self.b], "remove_effect", effect_id=key),
            effect([self.b], "damage", amount=20)), self.a, 26)
        self.assertEqual(self.players[1].hp, 100)
        self.assertTrue(any("整项要求未执行" in r.get("text", "") for r in records))

    def test_duplicate_revisions_rejected(self):
        self.runtime.apply(plan(self.link()), self.a, 25)
        key = next(iter(self.runtime.active))
        rev = Revision(effect_id=key, replacement=self.link("改名"))
        candidate = DeclarationPlan(requirements=[Requirement(quote="测试", status="supported", revisions=[rev, rev])])
        with self.assertRaises(ValueError):
            validate_plan(candidate, "测试", self.room.players, {key})

    def test_revision_cannot_overfill_another_player(self):
        self.runtime.apply(plan(self.link()), self.a, 25)
        key = next(iter(self.runtime.active))
        for i in range(self.room.pack.attributes.max_count):
            attr = attributes.create_attribute(owner_id=self.a, name=f"已有{i}", attr_type="utility",
                value=1, cap=1, narrative="", turn_no=1, max_cap=10)
            self.players[0].attributes[attr.attr_id] = attr
        replacement = self.link()
        replacement.targets = [self.a]
        candidate = DeclarationPlan(requirements=[Requirement(quote="测试", status="supported",
            revisions=[Revision(effect_id=key, replacement=replacement)])])
        self.runtime.apply(candidate, self.a, 26)
        self.assertEqual(self.runtime.active[key].spec.targets, [self.b, self.c])

    def test_dead_player_does_not_satisfy_activation_condition(self):
        self.players[0].hp = 50
        spec = effect([self.a], "heal", amount=10, trigger="global_start", unit="permanent")
        spec.condition = Condition(players=[self.b], hp_below_pct=50)
        self.runtime.apply(plan(spec), self.a, 25)
        self.players[1].alive = False
        self.players[1].hp = 0
        self.runtime.start_turn(26, self.c)
        self.assertEqual(self.players[0].hp, 50)

    def test_renewal_does_not_restore_shield_charges(self):
        spec = effect([self.b], "shield", trigger="passive", unit="global_turn", count=10, charges=2)
        self.runtime.apply(plan(spec), self.a, 25)
        key = next(iter(self.runtime.active))
        self.hit(self.a, self.b)
        candidate = DeclarationPlan(requirements=[Requirement(quote="测试", status="supported",
            revisions=[Revision(effect_id=key, replacement=spec, renew=True)])])
        self.runtime.apply(candidate, self.a, 31)
        self.assertEqual(self.runtime.active[key].charges[f"{self.b}:0"], 1)

    def test_expired_trigger_in_same_event_does_not_fire(self):
        first = effect([self.b], "damage", amount=10, trigger="after_damage",
                       trigger_player=self.a, unit="permanent")
        second = effect([self.c], "damage", amount=10, trigger="after_damage",
                        trigger_player=self.a, unit="permanent")
        second.end_condition = Condition(players=[self.b], hp_below_pct=95)
        self.runtime.apply(plan(first, second), self.a, 25)
        self.hit(self.c, self.a, amount=1)
        self.assertEqual(self.players[1].hp, 90)
        self.assertEqual(self.players[2].hp, 100)

    def test_reordering_shield_actions_does_not_restore_charges(self):
        shield = effect([self.b], "shield", trigger="passive", unit="permanent", charges=2)
        shield.actions.append(Action(kind="reduction", amount=50))
        self.runtime.apply(plan(shield), self.a, 25)
        key = next(iter(self.runtime.active))
        self.hit(self.a, self.b)
        replacement = shield.model_copy(deep=True)
        replacement.actions.reverse()
        replacement.actions[1].lethal_only = True
        candidate = DeclarationPlan(requirements=[Requirement(quote="测试", status="supported",
            revisions=[Revision(effect_id=key, replacement=replacement)])])
        self.runtime.apply(candidate, self.a, 31)
        self.assertEqual(self.runtime.active[key].charges[f"{self.b}:1"], 1)

    def test_periodic_effects_cannot_multiply_transaction_budget(self):
        specs = [effect([self.b], "damage", amount=999, trigger="global_start",
                        unit="permanent", name=f"周期{i}") for i in range(3)]
        self.runtime.apply(plan(*specs), self.a, 1)
        records = self.runtime.start_turn(2, self.c)
        loss = sum(-r["hp_delta"] for r in records if r["kind"] == "hp")
        self.assertEqual(loss, self.runtime.hard())
        self.assertTrue(any(r["kind"] == "limited" for r in records))

    def test_duplicate_quote_cannot_execute_same_requirement_twice(self):
        req = Requirement(quote="攻击乙", status="supported", effects=[effect([self.b], "damage", amount=3)])
        candidate = DeclarationPlan(requirements=[req, req])
        with self.assertRaises(ValueError):
            validate_plan(candidate, "攻击乙", self.room.players, set())
        validate_plan(candidate, "攻击乙，攻击乙", self.room.players, set())
