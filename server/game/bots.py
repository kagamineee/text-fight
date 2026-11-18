"""假人：分难度的模板发言 + 种子化随机。

四档难度只改变"怎么打"，不改变规则：假人和真人一样，写一段自由文本
交给裁判，绝不直接改数值。

- easy    简单：乱挑目标、力道小，几乎不设防；
- normal  普通：专挑残血、受伤会包扎、没防具时会立个盾；
- hard    困难：伤害更高、建立定时护甲、强化通用护甲、低血优先自救；
- inferno 炼狱：追求斩杀、拆掉对手的属性、能连招，血线管理最冷静。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import debug_log  # [TEMP-LOG] 临时调试日志，稳定后与调用点一起删除
from .formulas import IntensityRules
from .models import Declaration, Player

DIFFICULTIES = ("easy", "normal", "hard", "inferno")
DEFAULT_DIFFICULTY = "normal"


def normalize_difficulty(value: str | None) -> str:
    return value if value in DIFFICULTIES else DEFAULT_DIFFICULTY


@dataclass(frozen=True)
class _Limits:
    cap: int
    hard: int
    max_attrs: int
    max_initial: int


def _limits(pack, turn_no: int) -> _Limits:
    if pack is None:
        cap = 8 + 2 * max(0, turn_no - 1)
        return _Limits(cap=cap, hard=max(1, round(cap * 1.5)), max_attrs=10, max_initial=10)
    rules = IntensityRules(
        cap_base=pack.intensity.cap_base,
        cap_growth=pack.intensity.cap_growth,
        ceiling_factor=pack.intensity.ceiling_factor,
    )
    return _Limits(
        cap=rules.cap(turn_no),
        hard=rules.hard(turn_no),
        max_attrs=pack.attributes.max_count,
        max_initial=pack.attributes.max_initial,
    )


# ---------------------------------------------------------------- 模板
EASY_ATTACK = [
    "我抬手拍了{target}一下，造成 {dmg} 点伤害。",
    "我冲过去撞了{target}一下，造成 {dmg} 点伤害。",
    "我踢了{target}一脚，造成 {dmg} 点伤害。",
    "我把杯子砸向{target}，造成 {dmg} 点伤害。",
]

NORMAL_ATTACK = [
    "我抓住{target}的衣领，一拳打在他胸口，造成 {dmg} 点伤害。",
    "我抬腿踹向{target}的膝盖，造成 {dmg} 点伤害。",
    "我拎起椅子砸向{target}，造成 {dmg} 点伤害。",
    "我照着{target}的肚子来了一下重的，造成 {dmg} 点伤害。",
]

HARD_ATTACK = [
    "我侧身逼近{target}，一肘砸在他的肋骨上，造成 {dmg} 点伤害。",
    "我抓住{target}收手的空档，连续两拳打在他脸上，造成 {dmg} 点伤害。",
    "我抄起椅子腿，劈头砸向{target}，造成 {dmg} 点伤害。",
]

HARD_ATTR_ATTACK = "我握紧「{attr}」，一击劈在{target}身上，造成 {dmg} 点伤害。"

INFERNO_ATTACK = [
    "我踏前一步，把全身力道灌进这一拳，砸在{target}的咽喉，造成 {dmg} 点伤害。",
    "我咬住{target}的破绽，一刀斩向他的要害，造成 {dmg} 点伤害。",
    "我把{target}摔向地面，紧跟着补上一脚，造成 {dmg} 点伤害。",
]

INFERNO_FINISH = "我一步踏进{target}的怀里，全力一拳砸在他的要害，造成 {dmg} 点伤害。"

EASY_HEAL = "我缩到角落，给自己包扎了一下，恢复 {n} 点血量。"
NORMAL_HEAL = "我撕下衣角按住伤口，给自己包扎，恢复 {n} 点血量。"
HARD_HEAL = "我咬牙给自己止血包扎，恢复 {n} 点血量。"
INFERNO_HEAL = "我冷冷退开半步，飞快处理伤口，恢复 {n} 点血量。"

FREEZE_ATTR = "我猛然挥出寒霜冲击，凝聚「冰封咒」冻住{target}，让他 {turns} 回合动弹不得。"
CREATE_ARMOR = "我为自己凝聚「{name}」，固定抵扣 {value} 点伤害，持续 {turns} 回合。"
UPGRADE_ARMOR = "我将自己的护甲「{attr}」（{effect_id}）的固定抵扣提高到 {value} 点，其他规则与剩余时限不变。"
DISPEL_ATTACK = "我解除{target}身上的「{attr}」（{effect_id}），随后对其造成 {dmg} 点伤害。"


def _best_attr(player: Player, attr_type: str):
    attrs = [a for a in player.attributes.values() if a.attr_type == attr_type]
    return max(attrs, key=lambda a: (a.value, a.cap), default=None)


class BotBrain:
    def __init__(
        self,
        seed: int,
        difficulty: str = DEFAULT_DIFFICULTY,
    ):
        self.rng = random.Random(seed)
        self.difficulty = normalize_difficulty(difficulty)
        self.effects = None

    def _alive_enemies(self, me: Player, players: dict[str, Player]) -> list[Player]:
        return [p for p in players.values() if p.alive and p.player_id != me.player_id]

    @staticmethod
    def _hp_ratio(me: Player) -> float:
        return me.hp / max(1, me.hp_max)

    @staticmethod
    def _lowest(enemies: list[Player]) -> Player:
        return min(enemies, key=lambda p: (p.hp, p.seat))

    def declare(
        self,
        me: Player,
        players: dict[str, Player],
        turn_no: int,
        pack=None,
        effects=None,
    ) -> Declaration:
        self.effects = effects
        enemies = self._alive_enemies(me, players)
        if effects is not None:
            allowed = effects.allowed_targets(me.player_id)
            enemies = [p for p in enemies if p.player_id in allowed]
        # 模板按名字明确指向玩家；重名不能猜目标，也不能把这一回合交给拒绝分支。
        population = effects.players.values() if effects is not None else players.values()
        names = [p.name for p in population if p.alive]
        enemies = [p for p in enemies if names.count(p.name) == 1]
        if not enemies:
            return Declaration(me.player_id, NORMAL_HEAL.format(n=1))
        lim = _limits(pack, turn_no)
        if self.difficulty == "easy":
            decl = self._declare_easy(me, enemies, lim)
        elif self.difficulty == "hard":
            decl = self._declare_hard(me, enemies, lim)
        elif self.difficulty == "inferno":
            decl = self._declare_inferno(me, enemies, lim)
        else:
            decl = self._declare_normal(me, enemies, lim)
        debug_log.log("bot_declare", turn=turn_no, player=me.name,
                      difficulty=self.difficulty, text=decl.content[:120])  # [TEMP-LOG]
        return decl

    def _count(self, player):
        return len(player.attributes) + (sum(player.player_id in e.spec.targets
            for e in self.effects.active.values()) if self.effects else 0)

    def _defended(self, player):
        return any(a.value > 0 and (a.attr_type == "defense" or a.shield or a.damage_reduction)
                   for a in player.attributes.values()) or bool(self.effects and any(
            any(self.effects.passives(player.player_id, kind)) for kind in ("armor", "shield", "reduction")))

    def _frozen(self, player):
        return any(a.freeze for a in player.attributes.values()) or bool(
            self.effects and self.effects.frozen(player.player_id))

    def _upgrade(self, me, lim):
        if self.effects:
            for effect, _, action in self.effects.passives(me.player_id, "armor"):
                if (not effect.spec.protected and effect.spec.targets == [me.player_id]
                        and action.amount < lim.max_initial):
                    return Declaration(me.player_id, UPGRADE_ARMOR.format(attr=effect.spec.name,
                        effect_id=effect.effect_id, value=min(lim.max_initial, action.amount + 3)))
        return None

    def _dispel(self, me, enemies, lim):
        candidates = []
        for enemy in enemies:
            for attr in enemy.attributes.values():
                if (not attr.indestructible and not attr.freeze and attr.per_turn >= 0
                        and attr.value > 0):
                    candidates.append((bool(attr.shield), attr.value, enemy, attr.name, attr.attr_id))
            if self.effects:
                for effect in self.effects.active.values():
                    spec = effect.spec
                    if (enemy.player_id in spec.targets and me.player_id not in spec.targets
                            and not spec.protected
                            and all(a.kind in ("armor", "shield", "reduction", "heal") for a in spec.actions)):
                        candidates.append((any(a.kind == "shield" for a in spec.actions),
                            sum(a.amount or 1 for a in spec.actions), enemy, spec.name, effect.effect_id))
        if not candidates:
            return None
        _, _, enemy, name, key = max(candidates, key=lambda item: item[:2])
        return Declaration(me.player_id, DISPEL_ATTACK.format(target=enemy.name, attr=name,
            effect_id=key, dmg=max(1, round(lim.hard * 0.8))))

    # ------------------------------------------------------------ 简单
    def _declare_easy(self, me: Player, enemies: list[Player], lim: _Limits) -> Declaration:
        roll = self.rng.random()
        if self._hp_ratio(me) < 0.3 and roll < 0.25:
            n = max(1, min(lim.hard, 3 + self.rng.randint(0, 2)))
            return Declaration(me.player_id, EASY_HEAL.format(n=n))
        target = self.rng.choice(enemies)
        dmg = max(1, min(lim.hard, 2 + self.rng.randint(0, 3)))
        return Declaration(
            me.player_id,
            self.rng.choice(EASY_ATTACK).format(target=target.name, dmg=dmg),
        )

    # ------------------------------------------------------------ 普通
    def _declare_normal(
        self, me: Player, enemies: list[Player], lim: _Limits
    ) -> Declaration:
        roll = self.rng.random()
        target = self._lowest(enemies)
        if self._hp_ratio(me) < 0.4 and roll < 0.6:
            n = max(1, min(lim.hard, round(lim.cap * 0.6)))
            return Declaration(me.player_id, NORMAL_HEAL.format(n=n))
        if (
            not self._defended(me)
            and self._count(me) < lim.max_attrs
            and roll < 0.3
        ):
            return Declaration(
                me.player_id,
                CREATE_ARMOR.format(
                    name="布衣护甲",
                    turns=4,
                    value=max(2, min(lim.max_initial, round(lim.cap * 0.5))),
                ),
            )
        dmg = max(3, min(lim.hard, round(lim.cap * 0.65)))
        return Declaration(
            me.player_id,
            self.rng.choice(NORMAL_ATTACK).format(target=target.name, dmg=dmg),
        )

    # ------------------------------------------------------------ 困难
    def _declare_hard(self, me: Player, enemies: list[Player], lim: _Limits) -> Declaration:
        roll = self.rng.random()
        target = self._lowest(enemies)
        if self._hp_ratio(me) < 0.45 and roll < 0.65:
            n = max(1, min(lim.hard, round(lim.cap * 0.9)))
            return Declaration(me.player_id, HARD_HEAL.format(n=n))

        # 血线健康时冻住最强敌人，抢两个回合的主动权
        if (
            self._hp_ratio(me) > 0.5
            and not self._frozen(target)
            and self._count(target) < lim.max_attrs
            and 0.65 <= roll < 0.75
        ):
            return Declaration(
                me.player_id, FREEZE_ATTR.format(target=target.name, turns=2)
            )

        atk = _best_attr(me, "attack")
        upgrade = self._upgrade(me, lim) if roll < 0.4 else None
        if upgrade:
            return upgrade

        if self._count(me) < lim.max_attrs and roll < 0.55:
            if not self._defended(me):
                return Declaration(
                    me.player_id,
                    CREATE_ARMOR.format(
                        name="玄铁护甲",
                        turns=4,
                        value=min(lim.max_initial, max(3, round(lim.cap * 0.6))),
                    ),
                )


        dmg = max(4, min(lim.hard, round(lim.hard * 0.85)))
        if atk is not None:
            return Declaration(
                me.player_id,
                HARD_ATTR_ATTACK.format(attr=atk.name, target=target.name, dmg=dmg),
            )
        return Declaration(
            me.player_id,
            self.rng.choice(HARD_ATTACK).format(target=target.name, dmg=dmg),
        )

    # ------------------------------------------------------------ 炼狱
    def _declare_inferno(
        self, me: Player, enemies: list[Player], lim: _Limits
    ) -> Declaration:
        roll = self.rng.random()
        target = self._lowest(enemies)

        if self._hp_ratio(me) < 0.3 and roll < 0.5:
            n = max(1, min(lim.hard, round(lim.cap * 1.2)))
            return Declaration(me.player_id, INFERNO_HEAL.format(n=n))

        dispel = self._dispel(me, enemies, lim) if roll < 0.45 else None
        if dispel:
            return dispel

        # 炼狱：对手没被冻时优先抢控制权，冻住之后再安心输出
        if (
            not self._frozen(target)
            and self._count(target) < lim.max_attrs
            and 0.5 <= roll < 0.62
        ):
            return Declaration(
                me.player_id, FREEZE_ATTR.format(target=target.name, turns=2)
            )

        upgrade = self._upgrade(me, lim) if roll < 0.6 else None
        if upgrade:
            return upgrade

        if (
            self._count(me) < lim.max_attrs
            and not self._defended(me)
            and roll < 0.85
        ):
            return Declaration(
                me.player_id,
                CREATE_ARMOR.format(
                    name="金刚护甲",
                    turns=4,
                    value=min(lim.max_initial, max(3, round(lim.cap * 0.6))),
                ),
            )

        dmg = lim.hard
        template = INFERNO_FINISH if target.hp <= dmg else self.rng.choice(INFERNO_ATTACK)
        return Declaration(
            me.player_id,
            template.format(target=target.name, dmg=dmg),
        )
