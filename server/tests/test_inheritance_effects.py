"""通过真实死亡结算验证新旧能力继承，不模拟继承函数。"""
import asyncio
import unittest

from server.game import inheritance
from server.game.effect_plan import Action, Condition, DeclarationPlan, EffectSpec, Lifetime, Requirement
from server.tests import test_p2

make_room = test_p2.make_room


def plan(*specs):
    return DeclarationPlan(requirements=[Requirement(quote="测试", status="supported", effects=list(specs))])


def positive(pid, kind="armor", amount=10, *, trigger="passive", count=None, **kwargs):
    action = Action(kind=kind, **({"amount": amount} if kind != "shield" else {}), **kwargs)
    return EffectSpec(name="测试能力", targets=[pid], trigger=trigger,
                      trigger_player=pid if trigger in ("owner_start", "after_damage", "after_block") else None,
                      lifetime=Lifetime(unit="global_turn", count=count) if count else Lifetime(unit="permanent"),
                      actions=[action])


class EffectInheritanceTest(unittest.TestCase):
    def setUp(self):
        self.room, self.players, self.hub = make_room(3)
        self.a, self.b, self.c = self.players
        self.room.turn_no = 10

    def give(self, *specs):
        self.room.effects.apply(plan(*specs), self.b.player_id, 1)

    def kill(self, source=None):
        self.b.hp = 1
        self.room.effects.begin(10)
        self.room.effects.damage(self.b.player_id, 100, source or self.a.player_id, "击杀", pierce=True)
        asyncio.run(self.room._publish_effect_records(list(self.room.effects.records)))

    def inherited(self):
        return [e for e in self.room.effects.active.values() if self.a.player_id in e.spec.targets]

    def test_death_cleanup_keeps_positive_candidate_and_real_defense(self):
        self.give(positive(self.b.player_id, count=10))
        self.room.effects.end_turn(2, self.c.player_id)
        self.kill()
        got, = self.inherited()
        self.assertEqual(got.remaining, 9)
        self.assertEqual(got.spec.actions[0].amount, 4)
        self.assertEqual(got.source, self.a.player_id)
        self.assertNotIn(self.b.player_id, got.spec.targets)
        self.room.effects.begin(11)
        self.assertEqual(self.room.effects.damage(self.a.player_id, 10, self.c.player_id, "攻击"), 6)
        self.assertEqual(sum(m.get("type") == "inheritance" for m in self.hub.messages), 1)
        self.assertEqual(self.room.snapshot_for(self.a.player_id)["state"]["active_effects"][0]["effect_id"], got.effect_id)

    def test_negative_and_mixed_generic_effects_are_not_candidates(self):
        for actions in ([Action(kind="skip_action")],
                        [Action(kind="restrict_targets", allowed=[self.a.player_id, self.b.player_id])],
                        [Action(kind="armor", amount=99), Action(kind="skip_action")]):
            with self.subTest(actions=actions):
                self.room.effects.reset()
                self.give(EffectSpec(name="混合诅咒", targets=[self.b.player_id], trigger="passive",
                    lifetime=Lifetime(unit="permanent"), actions=actions))
                self.assertIsNone(inheritance.pick_candidate(self.b, self.room.effects.active.values(), 10))

    def test_poison_drain_and_hostile_triggers_never_transfer(self):
        for action in [Action(kind="damage", amount=90),
                       Action(kind="drain", amount=90, beneficiary=self.a.player_id),
                       Action(kind="remove_effect", effect_id="legacy")]:
            spec = EffectSpec(name="负面", targets=[self.b.player_id], trigger="global_start",
                              lifetime=Lifetime(unit="permanent"), actions=[action])
            from server.game.effect_runtime import ActiveEffect
            active = ActiveEffect("bad", self.a.player_id, spec, 1, None)
            self.assertIsNone(inheritance.pick_candidate(self.b, [active], 10))

    def test_legacy_curse_cannot_be_laundered_into_positive_shell(self):
        bad = test_p2.InheritanceTest()._give_attr(self.b, 99, 100, "高强度诅咒")
        good = test_p2.InheritanceTest()._give_attr(self.b, 3, 4, "小剑")
        for freeze, per_turn in [(True, 0), (False, -10), (True, 10)]:
            bad.freeze, bad.per_turn = freeze, per_turn
            self.assertIs(inheritance.pick_legacy(self.b), good)
        bad.freeze, bad.per_turn, bad.value = False, 0, 0
        self.assertIs(inheritance.pick_legacy(self.b), good)
        self.kill()
        self.assertEqual(len(self.a.attributes), 1)
        self.assertTrue(next(iter(self.a.attributes.values())).name.startswith("小剑"))

    def test_generic_and_legacy_compete_for_one_slot(self):
        self.give(positive(self.b.player_id, amount=10))
        test_p2.InheritanceTest()._give_attr(self.b, 3, 4)
        self.kill()
        self.assertEqual(len(self.inherited()), 1)
        self.assertFalse(self.a.attributes)

    def test_only_remaining_shield_charges_are_scaled(self):
        self.give(positive(self.b.player_id, "shield", charges=5))
        self.room.effects.begin(2)
        self.room.effects.damage(self.b.player_id, 1, self.c.player_id, "试探")
        self.room.effects.damage(self.b.player_id, 1, self.c.player_id, "试探")
        self.kill()
        got, = self.inherited()
        self.assertEqual(got.spec.actions[0].charges, 1)
        self.assertEqual(list(got.charges.values()), [1])
        self.room.effects.begin(11)
        self.assertEqual(self.room.effects.damage(self.a.player_id, 10, self.c.player_id, "试探"), 0)
        self.assertNotIn(got.effect_id, self.room.effects.active)

    def test_empty_shield_in_mixed_positive_effect_not_refilled(self):
        spec = positive(self.b.player_id, "shield", charges=1)
        spec.actions.append(Action(kind="armor", amount=10))
        self.give(spec)
        self.room.effects.begin(2)
        self.room.effects.damage(self.b.player_id, 1, self.c.player_id, "试探")
        self.kill()
        got, = self.inherited()
        self.assertEqual([a.kind for a in got.spec.actions], ["armor"])

    def test_healing_clock_and_condition_rebind_to_killer(self):
        spec = positive(self.b.player_id, "heal", trigger="owner_start", amount=10)
        spec.lifetime = Lifetime(unit="owner_turn", count=3, clock_player=self.b.player_id)
        spec.condition = Condition(players=[self.b.player_id], hp_below_pct=90)
        self.give(spec)
        self.kill()
        got, = self.inherited()
        self.assertEqual(got.spec.trigger_player, self.a.player_id)
        self.assertEqual(got.spec.lifetime.clock_player, self.a.player_id)
        self.a.hp = 50
        self.room.effects.start_turn(11, self.a.player_id)
        self.assertEqual(self.a.hp, 54)
        self.room.effects.end_turn(11, self.a.player_id)
        self.assertEqual(got.remaining, 2)

    def test_foreign_condition_not_removed_to_force_inheritance(self):
        spec = positive(self.b.player_id)
        spec.condition = Condition(players=[self.c.player_id], hp_below_pct=50)
        self.give(spec)
        self.kill()
        self.assertFalse(self.inherited())

    def test_shared_buff_only_transfers_victims_share(self):
        spec = positive(self.b.player_id)
        spec.targets.append(self.c.player_id)
        self.give(spec)
        self.kill()
        got, = self.inherited()
        self.assertEqual(got.spec.targets, [self.a.player_id])
        self.assertEqual(len(list(self.room.effects.passives(self.c.player_id, "armor"))), 1)

    def test_periodic_kill_retains_current_credit_and_positive_inheritance(self):
        self.give(positive(self.b.player_id))
        poison = EffectSpec(name="毒", targets=[self.b.player_id], trigger="global_start",
            lifetime=Lifetime(unit="permanent"), actions=[Action(kind="damage", amount=30)])
        self.room.effects.apply(plan(poison), self.a.player_id, 1)
        self.b.hp = 1
        records = self.room.effects.start_turn(10, self.c.player_id)
        asyncio.run(self.room._publish_effect_records(records))
        got, = self.inherited()
        self.assertEqual([a.kind for a in got.spec.actions], ["armor"])

    def test_full_generic_capacity_skips_inheritance(self):
        for i in range(self.room.pack.attributes.max_count):
            self.room.effects.apply(plan(positive(self.a.player_id, amount=1)), self.a.player_id, 1)
        self.give(positive(self.b.player_id))
        self.kill()
        self.assertEqual(len(self.inherited()), self.room.pack.attributes.max_count)
        self.assertFalse([m for m in self.hub.messages if m.get("type") == "inheritance"])

    def test_end_condition_is_preserved_and_already_ended_transfer_skips(self):
        spec = positive(self.b.player_id, amount=10)
        spec.end_condition = Condition(players=[self.b.player_id], hp_below_pct=50)
        self.give(spec)
        self.a.hp = 40
        self.room.effects.begin(10)
        self.room.effects.damage(self.b.player_id, 200, self.a.player_id, "击杀")
        asyncio.run(self.room._publish_effect_records(list(self.room.effects.records)))
        self.assertFalse(self.inherited())
        self.assertTrue(any(e.payload.get("granted") is False for e in self.room.ledger))

    def test_round_progress_is_transferred_without_starting_fresh_round(self):
        spec = positive(self.b.player_id)
        spec.lifetime = Lifetime(unit="round", count=2)
        self.give(spec)
        self.room.effects.end_turn(2, self.c.player_id)
        self.kill()
        got, = self.inherited()
        self.assertEqual(got.round_waiting, {self.a.player_id})
        self.room.effects.end_turn(10, self.a.player_id)
        self.assertEqual(got.remaining, 1)

    def test_global_capacity_and_dead_killer_drop_saved_candidates(self):
        self.room.effects.MAX_ACTIVE = 1
        spec = positive(self.b.player_id)
        spec.targets.append(self.c.player_id)
        self.give(spec)
        self.kill()
        self.assertFalse(self.inherited())
        self.assertFalse(self.room.effects.death_legacies)

    def test_suicide_does_not_transfer_generic_effect_or_leave_snapshot(self):
        self.give(positive(self.b.player_id))
        self.kill(self.b.player_id)
        self.assertFalse(self.room.effects.active)
        self.assertFalse(self.room.effects.death_legacies)

    def test_legacy_snapshot_is_not_changed_by_later_mutation(self):
        attr = test_p2.InheritanceTest()._give_attr(self.b, 10, 20)
        self.room.effects.begin(10)
        self.b.hp = 1
        self.room.effects.damage(self.b.player_id, 100, self.a.player_id, "击杀")
        attr.value = 1
        asyncio.run(self.room._publish_effect_records(list(self.room.effects.records)))
        self.assertEqual(next(iter(self.a.attributes.values())).value, 4)

    def test_inherited_names_avoid_generic_and_legacy_collisions(self):
        owned = positive(self.a.player_id, amount=1)
        owned.name = "烈焰之心·遗"
        self.room.effects.apply(plan(owned), self.a.player_id, 1)
        test_p2.InheritanceTest()._give_attr(self.b, 10, 20)
        self.kill()
        self.assertEqual(next(iter(self.a.attributes.values())).name, "烈焰之心·遗·2")
