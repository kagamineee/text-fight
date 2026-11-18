"""确定性效果解释器：输入已校验计划，输出真实状态变化，不调用模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

from .effect_plan import DeclarationPlan, EffectSpec
from . import inheritance
from .formulas import IntensityRules, effective_defense, resolve_damage, apply_armor_wear


@dataclass
class ActiveEffect:
    effect_id: str
    source: str
    spec: EffectSpec
    opened_turn: int
    remaining: int | None
    charges: dict[str, int] = field(default_factory=dict)
    round_waiting: set[str] = field(default_factory=set)


class EffectRuntime:
    MAX_ACTIVE = 64
    MAX_EVENTS = 128
    MAX_DEPTH = 8

    def __init__(self, players, pack):
        self.players = players
        self.pack = pack
        self.active: dict[str, ActiveEffect] = {}
        self.serial = 0
        self.turn = 0
        self.records: list[dict] = []
        self.queue = deque()
        self.fired: set[str] = set()
        self.depth = 0
        self.budgets = {"damage": 0, "heal": 0}
        self.death_legacies = {}

    def reset(self):
        self.death_legacies.clear()
        self.active.clear()
        self.serial = 0
        self.queue.clear()
        self.records.clear()
        self.fired.clear()
        self.depth = 0
        self.turn = 0
        self.budgets = {"damage": 0, "heal": 0}

    def hard(self):
        cfg = self.pack.intensity
        return IntensityRules(cfg.cap_base, cfg.cap_growth, cfg.ceiling_factor).hard(self.turn)

    def begin(self, turn):
        self.turn = turn
        self.records = []
        self.queue.clear()
        self.fired.clear()
        self.depth = 0
        self.budgets = {"damage": self.hard(), "heal": self.hard()}
        self.expire_conditions()

    def condition(self, cond, *, ending=False):
        return cond is None or any(
            (ending and (pid not in self.players or not self.players[pid].alive))
            or (pid in self.players and self.players[pid].alive
                and self.players[pid].hp * 100 < self.players[pid].hp_max * cond.hp_below_pct)
            for pid in cond.players)

    def expire_conditions(self):
        for effect in list(self.active.values()):
            spec = effect.spec
            if (not any(self.players[pid].alive for pid in spec.targets)
                    or (spec.end_condition is not None and self.condition(spec.end_condition, ending=True))
                    or (spec.lifetime.clock_player and not self.players[spec.lifetime.clock_player].alive)
                    or (spec.trigger_player and not self.players[spec.trigger_player].alive)):
                self.remove(effect.effect_id, "condition")

    def remove(self, effect_id, reason, actor=None):
        effect = self.active.get(effect_id)
        if effect:
            if reason == "dispel" and effect.spec.protected:
                self.records.append({"kind": "blocked", "text": f"「{effect.spec.name}」不可提前解除。"})
                return
            del self.active[effect_id]
            endings = {"dispel": "提前解除", "expired": "到期", "condition": "满足结束条件",
                       "charges": "耗尽格挡次数"}
            self.records.append({"kind": "removed", "effect_id": effect_id, "reason": reason,
                                 "text": f"「{effect.spec.name}」已{endings.get(reason, reason)}。"})
            if reason == "dispel":
                self.queue.append(("removed", None, effect_id, self.depth + 1))
            return
        # 旧包的属性是既有状态，同样允许显式引用；不可破坏规则不绕过。
        for player in self.players.values():
            attr = player.attributes.get(effect_id)
            if attr:
                if attr.indestructible:
                    self.records.append({"kind": "blocked", "text": f"「{attr.name}」不可提前解除。"})
                else:
                    del player.attributes[effect_id]
                    self.records.append({"kind": "removed", "effect_id": effect_id,
                                         "text": f"「{attr.name}」已解除。"})
                    self.queue.append(("removed", None, effect_id, self.depth + 1))
                return
        self.records.append({"kind": "blocked", "text": "要解除的效果已不存在。"})

    def passives(self, pid, kind):
        for effect in list(self.active.values()):
            if (pid in effect.spec.targets and effect.spec.trigger == "passive"
                    and self.condition(effect.spec.condition)):
                for index, action in enumerate(effect.spec.actions):
                    if action.kind == kind:
                        key = f"{pid}:{index}"
                        if action.kind == "shield" and action.charges is not None and effect.charges.get(key, 0) <= 0:
                            continue
                        yield effect, index, action

    def allowed_targets(self, pid):
        allowed = {key for key, p in self.players.items() if p.alive and key != pid}
        for _, _, action in self.passives(pid, "restrict_targets"):
            allowed &= set(action.allowed)
        return allowed

    def can_damage(self, source, target):
        # 自伤属于代价，不是攻击第三人。
        return source == target or target in self.allowed_targets(source)

    def frozen(self, pid):
        return any(self.passives(pid, "skip_action"))

    def _emit_hp(self, pid, before, source, label):
        p = self.players[pid]
        actual = p.hp - before
        if not actual:
            return
        p.hp_peak = max(p.hp_peak, p.hp)
        died = p.hp == 0 and p.alive
        if died:
            # 在死亡清理前保存候选；只保存实际仍存在的正面状态。
            self.death_legacies[pid] = inheritance.pick_candidate(p, self.active.values(), self.turn)
            p.alive = False
        self.records.append({"kind": "hp", "player_id": pid, "hp_delta": actual,
                             "hp": p.hp, "source": source, "name": label, "dead": died})
        if actual < 0:
            self.queue.append(("after_damage", pid, None, self.depth + 1))
        # 跨过阈值立即解除，后续治疗不能复活已结束规则。
        self.expire_conditions()

    def heal(self, pid, amount, source, label):
        p = self.players.get(pid)
        if not p or not p.alive:
            return 0
        before = p.hp
        p.hp = min(p.hp_max, p.hp + max(0, amount))
        self._emit_hp(pid, before, source, label)
        return p.hp - before

    def damage(self, pid, amount, source, label, bypass_armor=False, pierce=False):
        p = self.players.get(pid)
        if not p or not p.alive or amount <= 0:
            return 0
        if not self.can_damage(source, pid):
            self.records.append({"kind": "blocked", "text": f"{self.players[source].name} 的攻击目标限制阻止了对 {p.name} 的伤害。"})
            return 0
        defense, used = effective_defense(p.attributes.values())
        generic_armor = list(self.passives(pid, "armor"))
        defense += sum(a.amount for _, _, a in generic_armor)
        reduction = max([a.damage_reduction for a in p.attributes.values()]
                        + [a.amount for _, _, a in self.passives(pid, "reduction")] + [0])
        projected = amount if bypass_armor else resolve_damage(amount, defense)
        if reduction:
            projected = max(1, round(projected * (100 - reduction) / 100))
        lethal = projected >= p.hp
        shields = [(e, i, a) for e, i, a in self.passives(pid, "shield")
                   if not a.lethal_only or lethal]
        legacy = next((a for a in p.attributes.values() if a.shield and
                       (not a.lethal_only or lethal)), None)
        protected = any(e.spec.protected for e, _, _ in shields) or any(
            a.shield and a.indestructible for a in p.attributes.values())
        if (shields or legacy) and (not pierce or protected):
            if shields:
                effect, index, action = shields[0]
                shield_name = effect.spec.name
                if action.charges is not None:
                    key = f"{pid}:{index}"
                    effect.charges[key] -= 1
                    if effect.charges[key] == 0:
                        # 每个对象、每个动作独立消耗，不删除同一能力的其他修饰动作。
                        if (all(a.kind == "shield" and a.charges is not None for a in effect.spec.actions)
                                and all(value == 0 for value in effect.charges.values())):
                            self.remove(effect.effect_id, "charges")
            else:
                shield_name = legacy.name
                if legacy.shield_charges:
                    legacy.shield_charges -= 1
                    if legacy.shield_charges == 0:
                        p.attributes.pop(legacy.attr_id, None)
                if legacy.block_heal_pct:
                    for other in self.players.values():
                        self.heal(other.player_id, max(1, round(other.hp_max * legacy.block_heal_pct / 100)),
                                  source, shield_name)
            self.records.append({"kind": "blocked", "player_id": pid, "hp_delta": 0,
                                 "hp": p.hp, "shield": shield_name,
                                 "text": f"{p.name} 的「{shield_name}」挡下伤害。"})
            self.queue.append(("after_block", pid, None, self.depth + 1))
            return 0
        if not bypass_armor:
            absorbed = amount - resolve_damage(amount, defense)
            apply_armor_wear(used, absorbed)
        before = p.hp
        p.hp = max(0, p.hp - projected)
        self._emit_hp(pid, before, source, label)
        return before - p.hp

    def execute_actions(self, spec, source, budgets):
        if not self.condition(spec.condition):
            return
        targets = [pid for pid in spec.targets if self.players[pid].alive]
        if spec.selection == "lowest_hp" and targets:
            targets = [min(targets, key=lambda pid: (self.players[pid].hp, self.players[pid].seat))]
        for action in spec.actions:
            if action.kind == "remove_effect":
                self.remove(action.effect_id, "dispel", source)
                continue
            for pid in targets:
                if not self.players[pid].alive:
                    continue
                amount = action.amount or 0
                if action.basis == "max_hp_pct":
                    amount = round(self.players[pid].hp_max * amount / 100)
                budget_key = "heal" if action.kind == "heal" else "damage"
                accepted = min(amount, budgets[budget_key])
                if action.kind == "drain":
                    accepted = min(accepted, budgets["heal"])
                if accepted < amount:
                    self.records.append({"kind": "limited", "text": f"「{spec.name}」本次强度预算限制：{amount} → {accepted}。"})
                budgets[budget_key] -= accepted
                if action.kind in ("damage", "drain"):
                    actual = self.damage(pid, accepted, source, spec.name, action.bypass_armor)
                    if action.kind == "drain":
                        budgets["heal"] -= actual
                        self.heal(action.beneficiary, actual, source, spec.name)
                elif action.kind == "heal":
                    self.heal(pid, accepted, source, spec.name)
        self.expire_conditions()

    def compile_spec(self, spec):
        """只允许明确的数值平衡调整；对象、触发、时长、结束条件保持原样。"""
        actions = []
        for action in spec.actions:
            if action.kind == "armor":
                cap = min(self.pack.attributes.max_initial, self.pack.attributes.max_cap)
                accepted = min(action.amount, cap)
                if accepted != action.amount:
                    self.records.append({"kind": "limited", "text": f"「{spec.name}」护甲受包上限限制：{action.amount} → {accepted}。"})
                action = action.model_copy(update={"amount": accepted})
            actions.append(action)
        return spec.model_copy(update={"actions": actions}, deep=True)

    def drain_queue(self):
        processed = 0
        while self.queue and processed < self.MAX_EVENTS:
            trigger, pid, effect_id, depth = self.queue.popleft()
            processed += 1
            if depth > self.MAX_DEPTH:
                self.records.append({"kind": "limited", "text": "触发链达到深度上限，剩余连锁未执行。"})
                continue
            self.depth = depth
            for effect in list(self.active.values()):
                spec = effect.spec
                if effect.effect_id not in self.active or spec.trigger != trigger:
                    continue
                if trigger == "removed" and spec.watch_effect != effect_id:
                    continue
                if trigger != "removed" and spec.trigger_player != pid:
                    continue
                if not self.condition(spec.condition):
                    continue
                if effect.effect_id in self.fired:
                    self.records.append({"kind": "limited", "text": f"「{spec.name}」本次结算已触发，重复连锁未执行。"})
                    continue
                self.fired.add(effect.effect_id)
                self.execute_actions(spec, effect.source, self.budgets)
                if trigger == "removed":
                    self.remove(effect.effect_id, "condition")
        if self.queue:
            self.records.append({"kind": "limited", "text": "触发事件达到安全上限，剩余连锁未执行。"})
            self.queue.clear()
        self.depth = 0

    def apply(self, plan: DeclarationPlan, source: str, turn: int):
        self.begin(turn)
        if not self.players[source].alive or self.frozen(source) or any(a.freeze for a in self.players[source].attributes.values()):
            self.records.append({"kind": "blocked", "text": "行动者已出局或被禁锢，本次计划未执行。"})
            return list(self.records)
        budgets = self.budgets
        aliases = {}
        for req in plan.requirements:
            if req.status != "supported":
                self.records.append({"kind": "unsupported", "text": f"未执行「{req.quote}」：{req.reason}"})
                continue
            invalid_revision = next((rev for rev in req.revisions
                if rev.effect_id not in self.active or self.active[rev.effect_id].spec.protected
                or (not rev.renew and rev.replacement.lifetime != self.active[rev.effect_id].spec.lifetime)), None)
            if invalid_revision:
                self.records.append({"kind": "blocked", "text": f"整项要求未执行：{invalid_revision.effect_id} 不存在或不可改写。"})
                continue
            specs = req.effects + [rev.replacement for rev in req.revisions]
            this_ids = {spec.local_id for spec in req.effects if spec.local_id}
            unavailable = next((spec.watch_effect for spec in specs if spec.watch_effect
                and spec.watch_effect not in this_ids
                and aliases.get(spec.watch_effect, spec.watch_effect) not in self.active
                and not any(aliases.get(spec.watch_effect, spec.watch_effect) in p.attributes
                            for p in self.players.values())), None)
            removal_ids = [aliases.get(a.effect_id, a.effect_id) for spec in specs for a in spec.actions
                           if a.kind == "remove_effect"]
            blocked_remove = next((key for key in removal_ids if not self.removable(key)), None)
            if unavailable or blocked_remove:
                self.records.append({"kind": "blocked", "text": f"整项要求未执行：引用的效果 {unavailable or blocked_remove} 不存在或不可解除。"})
                continue
            persistent = sum(spec.trigger != "immediate" for spec in req.effects)
            replacements = {rev.effect_id: rev.replacement for rev in req.revisions}
            affected = {pid for spec in specs if spec.trigger != "immediate" for pid in spec.targets}
            overflow = any(sum(pid in replacements.get(e.effect_id, e.spec).targets for e in self.active.values())
                           + len(self.players[pid].attributes)
                           + sum(pid in spec.targets and spec.trigger != "immediate" for spec in req.effects)
                           > self.pack.attributes.max_count for pid in affected)
            if len(self.active) + persistent > self.MAX_ACTIVE or overflow:
                self.records.append({"kind": "limited", "text": f"效果数达到上限，整项要求未执行：{req.quote}"})
                continue
            for spec in req.effects:
                if spec.local_id:
                    self.serial += 1
                    aliases[spec.local_id] = f"effect-{self.serial}"
            for rev in req.revisions:
                old = self.active[rev.effect_id]
                previous = old.spec
                old.spec = self.compile_spec(self.resolve_refs(rev.replacement, aliases))
                if rev.renew:
                    old.remaining = rev.replacement.lifetime.count
                    old.opened_turn = turn
                    old.round_waiting = {pid for pid, p in self.players.items() if p.alive}
                previous_shields = {a.kind: (i, a) for i, a in enumerate(previous.actions) if a.kind == "shield"}
                charges = {}
                for i, action in enumerate(old.spec.actions):
                    if action.kind != "shield" or action.charges is None:
                        continue
                    old_index, prior = previous_shields.get("shield", (None, None))
                    for pid in old.spec.targets:
                        unchanged = prior is not None and prior.charges == action.charges
                        charges[f"{pid}:{i}"] = (old.charges.get(f"{pid}:{old_index}", action.charges)
                                                 if unchanged else action.charges)
                old.charges = charges
                self.records.append({"kind": "updated", "effect_id": old.effect_id, "text": self.describe(old)})
            for raw_spec in req.effects:
                spec = self.compile_spec(self.resolve_refs(raw_spec, aliases))
                if spec.trigger == "immediate":
                    self.execute_actions(spec, source, budgets)
                else:
                    key = aliases.get(raw_spec.local_id)
                    if key is None:
                        self.serial += 1
                        key = f"effect-{self.serial}"
                    active = ActiveEffect(key, source, spec, turn, spec.lifetime.count,
                        round_waiting={pid for pid, p in self.players.items() if p.alive})
                    for i, action in enumerate(spec.actions):
                        if action.kind == "shield" and action.charges:
                            for pid in spec.targets:
                                active.charges[f"{pid}:{i}"] = action.charges
                    self.active[key] = active
                    self.records.append({"kind": "created", "effect_id": key,
                                         "text": self.describe(active)})
                self.expire_conditions()
                self.drain_queue()
        return list(self.records)

    def removable(self, key):
        if key in self.active:
            return not self.active[key].spec.protected
        for p in self.players.values():
            if key in p.attributes:
                return not p.attributes[key].indestructible
        return False

    def inherit(self, src, victim, killer, ratio):
        """继承不属于行动，不调用 apply，也不清空正在发布的结算记录。"""
        spec = inheritance.transfer_effect(src, killer, ratio,
            [e.spec.name for e in self.active.values() if killer.player_id in e.spec.targets])
        if spec.end_condition and self.condition(spec.end_condition, ending=True):
            return None
        self.serial += 1
        key = f"effect-{self.serial}"
        waiting = (src.round_waiting - {victim.player_id}) & {
            pid for pid, p in self.players.items() if p.alive}
        if victim.player_id in src.round_waiting:
            waiting.add(killer.player_id)
        inherited = ActiveEffect(key, killer.player_id, spec, src.opened_turn, src.remaining,
                                 round_waiting=waiting)
        inherited.charges = {f"{killer.player_id}:{i}": a.charges
                            for i, a in enumerate(spec.actions) if a.kind == "shield" and a.charges}
        self.active[key] = inherited
        return inherited

    @staticmethod
    def resolve_refs(spec, aliases):
        return spec.model_copy(update={"local_id": None,
            "watch_effect": aliases.get(spec.watch_effect, spec.watch_effect),
            "actions": [a.model_copy(update={"effect_id": aliases.get(a.effect_id, a.effect_id)})
                        for a in spec.actions]}, deep=True)

    def start_turn(self, turn, actor):
        self.begin(turn)
        # 同一行动开始的周期与连锁共享预算，拆成多个效果不能绕过上限。
        for effect in list(self.active.values()):
            if effect.effect_id not in self.active or effect.opened_turn >= turn:
                continue
            spec = effect.spec
            if spec.trigger == "global_start" or (spec.trigger == "owner_start" and spec.trigger_player == actor):
                self.execute_actions(spec, effect.source, self.budgets)
                self.drain_queue()
        return list(self.records)

    def observe_environment(self, turn, changes, *, continuing=False):
        """旧包/天灾已完成扣血，仅通知触发器，不重复扣血或广播原伤害。"""
        if not continuing:
            self.begin(turn)
        for change in changes:
            if change.get("delta", 0) < 0:
                self.queue.append(("after_damage", change["player_id"], None, 1))
        self.drain_queue()
        return list(self.records)

    def end_turn(self, turn, actor):
        self.begin(turn)
        for effect in list(self.active.values()):
            if effect.opened_turn >= turn or effect.remaining is None:
                continue
            lifetime = effect.spec.lifetime
            tick = lifetime.unit == "global_turn" or (lifetime.unit == "owner_turn" and lifetime.clock_player == actor)
            if lifetime.unit == "round":
                effect.round_waiting.discard(actor)
                effect.round_waiting &= {pid for pid, p in self.players.items() if p.alive}
                if not effect.round_waiting:
                    tick = True
                    effect.round_waiting = {pid for pid, p in self.players.items() if p.alive}
            if tick:
                effect.remaining -= 1
                if effect.remaining == 0:
                    self.remove(effect.effect_id, "expired")
        return list(self.records)

    def describe(self, effect):
        spec = effect.spec
        names = lambda ids: "、".join(self.players[pid].name for pid in ids)
        bits = []
        for i, a in enumerate(spec.actions):
            if a.kind == "restrict_targets":
                bits.append(f"仅攻击{names(a.allowed)}（自身不计入攻击对象，治疗/辅助不受限）")
            elif a.kind == "drain":
                bits.append(f"吸取{a.amount}{'点' if a.basis == 'fixed' else '%上限'}生命，实际吸取量治疗{names([a.beneficiary])}")
            elif a.kind == "shield":
                counts = [effect.charges.get(f"{pid}:{i}", 0) for pid in spec.targets]
                bits.append(("仅致命" if a.lethal_only else "全部") + "伤害格挡" +
                            (f"，剩余次数{counts}" if a.charges else ""))
            else:
                labels = {"damage": "伤害", "heal": "治疗", "skip_action": "跳过行动",
                          "armor": "护甲", "reduction": "百分比减伤", "remove_effect": "解除效果"}
                bits.append(labels.get(a.kind, a.kind) + (f" {a.amount}" if a.amount else "") +
                            ("%生命上限" if a.basis == "max_hp_pct" else "") +
                            (f" {a.effect_id}" if a.effect_id else ""))
            if a.bypass_armor:
                bits.append("忽略护甲（不穿透护盾）")
        units = {"global_turn": "次全局行动", "owner_turn": "次指定玩家行动机会", "round": "轮"}
        duration = f"剩余{effect.remaining}{units[spec.lifetime.unit]}" if effect.remaining is not None else "无固定时限"
        triggers = {"passive": "持续生效", "global_start": "每次全局行动开始", "owner_start": "指定玩家行动开始",
                    "after_damage": "受到伤害后", "after_block": "格挡后", "removed": "指定效果被提前解除时"}
        detail = triggers.get(spec.trigger, spec.trigger)
        if spec.selection == "lowest_hp":
            detail += "，仅作用于目标中当前血量最低者（同血量按座位顺序）"
        if spec.trigger in ("after_damage", "after_block", "removed"):
            detail += "，每次结算最多触发一次"
        if spec.trigger_player:
            detail += f"（{names([spec.trigger_player])}）"
        if spec.watch_effect:
            detail += f"（{spec.watch_effect}）"
        if spec.lifetime.clock_player:
            duration += f"（{names([spec.lifetime.clock_player])}）"
        if spec.condition:
            detail += f"，条件：{names(spec.condition.players)}任一血量低于{spec.condition.hp_below_pct}%"
        if spec.end_condition:
            duration += f"；{names(spec.end_condition.players)}任一血量低于{spec.end_condition.hp_below_pct}%或出局时解除"
        return f"{names(spec.targets)}「{spec.name}」：{detail}；{'；'.join(bits)}；{duration}" + ("；不可提前解除" if spec.protected else "")

    def view(self):
        units = {"global_turn": "次全局行动", "owner_turn": "次指定玩家行动机会", "round": "轮"}
        return [{"effect_id": e.effect_id, "source": e.source, "targets": e.spec.targets,
                 "name": e.spec.name, "description": self.describe(e), "remaining": e.remaining,
                 "remaining_text": f"剩余{e.remaining}{units[e.spec.lifetime.unit]}" if e.remaining is not None else "无固定时限",
                 "spec": e.spec.model_dump(), "opened_turn": e.opened_turn,
                 "charges": dict(e.charges), "round_waiting": sorted(e.round_waiting)}
                for e in self.active.values()]

    def context(self):
        return "\n".join(f"{item['effect_id']}: {item['description']}" for item in self.view()) or "无"
