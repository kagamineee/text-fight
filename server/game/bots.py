"""假人：分难度的模板发言 + 种子化随机。

四档难度只改变"怎么打"，不改变规则：假人和真人一样，写一段自由文本
交给裁判，绝不直接改数值。

- easy    简单：乱挑目标、力道小、经常犹豫，几乎不设防；
- normal  普通：专挑残血、受伤会包扎、没防具时会立个盾；
- hard    困难：伤害更高、会武装自己、会强化已有属性、低血优先自救；
- inferno 炼狱：追求斩杀、拆掉对手的属性、能连招，血线管理最冷静。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import debug_log  # [TEMP-LOG] 临时调试日志，稳定后与调用点一起删除
from .formulas import IntensityRules
from .models import Declaration, Player

DIFFICULTIES = ("easy", "normal", "hard", "inferno")
DIFFICULTY_LABELS = {
    "easy": "简单",
    "normal": "普通",
    "hard": "困难",
    "inferno": "炼狱",
}
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

GUARD_TEMPLATES = [
    "我往后退了两步，先把气缓过来。",
    "我抬起胳膊护住头，没急着还手。",
    "我把椅子挡在身前，盯着对面。",
]

HESITATE_TEMPLATES = [
    "我绕着他转圈，没敢下手。",
    "我大喊一声给自己壮胆，脚却没动。",
    "我犹豫了一下，退了半步。",
]

CREATE_ATTR = "我沉肩蓄力，凝聚「{name}」，强度 {value} 点。"
UPGRADE_ATTR = "我稳住气息，强化「{attr}」，把它提到 {value} 点。"
SABOTAGE_ATTR = "我一掌拍在{target}的「{attr}」上，把它打碎封印，造成 {dmg} 点伤害。"
FREEZE_ATTR = "我猛然挥出寒霜冲击，凝聚「冰封咒」冻住{target}，让他 {turns} 回合动弹不得。"


def _best_attr(player: Player, attr_type: str):
    attrs = [a for a in player.attributes.values() if a.attr_type == attr_type]
    return max(attrs, key=lambda a: (a.value, a.cap), default=None)


def _best_enemy_attr(enemies: list[Player]):
    """挑对面最值得拆的属性：免伤盾最优先（不拆就打不动），其次数值最大的。"""
    best = None
    for enemy in enemies:
        for attr in enemy.attributes.values():
            if best is None:
                best = (enemy, attr)
                continue
            if attr.shield and not best[1].shield:
                best = (enemy, attr)
            elif attr.shield == best[1].shield and attr.value > best[1].value:
                best = (enemy, attr)
    return best if best else (None, None)


class BotBrain:
    def __init__(
        self,
        seed: int,
        difficulty: str = DEFAULT_DIFFICULTY,
    ):
        self.rng = random.Random(seed)
        self.difficulty = normalize_difficulty(difficulty)

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
    ) -> Declaration:
        enemies = self._alive_enemies(me, players)
        if not enemies:
            return Declaration(me.player_id, "场上没别人了，我收手。")
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

    # ------------------------------------------------------------ 简单
    def _declare_easy(self, me: Player, enemies: list[Player], lim: _Limits) -> Declaration:
        roll = self.rng.random()
        if self._hp_ratio(me) < 0.3 and roll < 0.25:
            n = max(1, min(lim.hard, 3 + self.rng.randint(0, 2)))
            return Declaration(me.player_id, EASY_HEAL.format(n=n))
        if roll < 0.3:
            return Declaration(me.player_id, self.rng.choice(HESITATE_TEMPLATES))
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
            not _best_attr(me, "defense")
            and len(me.attributes) < lim.max_attrs
            and roll < 0.3
        ):
            return Declaration(
                me.player_id,
                CREATE_ATTR.format(
                    name="布衣护甲",
                    value=max(2, min(lim.max_initial, round(lim.cap * 0.5))),
                ),
            )
        if roll > 0.92:
            return Declaration(me.player_id, self.rng.choice(GUARD_TEMPLATES))
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
            and not any(a.freeze for a in target.attributes.values())
            and len(target.attributes) < lim.max_attrs
            and 0.65 <= roll < 0.75
        ):
            return Declaration(
                me.player_id, FREEZE_ATTR.format(target=target.name, turns=2)
            )

        atk = _best_attr(me, "attack")
        if atk is not None and atk.value < atk.cap and roll < 0.4:
            value = min(atk.cap, max(atk.value + 3, round(lim.cap * 0.8)))
            return Declaration(
                me.player_id, UPGRADE_ATTR.format(attr=atk.name, value=value)
            )

        if len(me.attributes) < lim.max_attrs and roll < 0.55:
            if atk is None:
                return Declaration(
                    me.player_id,
                    CREATE_ATTR.format(
                        name="断钢刀",
                        value=min(lim.max_initial, max(3, round(lim.cap * 0.7))),
                    ),
                )
            if _best_attr(me, "defense") is None:
                return Declaration(
                    me.player_id,
                    CREATE_ATTR.format(
                        name="玄铁护甲",
                        value=min(lim.max_initial, max(3, round(lim.cap * 0.6))),
                    ),
                )

        if roll > 0.94:
            return Declaration(me.player_id, self.rng.choice(GUARD_TEMPLATES))

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

        victim, victim_attr = _best_enemy_attr(enemies)
        if victim is not None and victim_attr is not None and roll < 0.45:
            dmg = max(5, min(lim.hard, round(lim.hard * 0.8)))
            return Declaration(
                me.player_id,
                SABOTAGE_ATTR.format(
                    target=victim.name, attr=victim_attr.name, dmg=dmg
                ),
            )

        # 炼狱：对手没被冻时优先抢控制权，冻住之后再安心输出
        if (
            not any(a.freeze for a in target.attributes.values())
            and len(target.attributes) < lim.max_attrs
            and 0.5 <= roll < 0.62
        ):
            return Declaration(
                me.player_id, FREEZE_ATTR.format(target=target.name, turns=2)
            )

        atk = _best_attr(me, "attack")
        if atk is not None and atk.value < atk.cap and roll < 0.6:
            value = min(atk.cap, max(atk.value + 4, round(lim.cap * 0.9)))
            return Declaration(
                me.player_id, UPGRADE_ATTR.format(attr=atk.name, value=value)
            )

        if len(me.attributes) < lim.max_attrs and atk is None and roll < 0.7:
            return Declaration(
                me.player_id,
                CREATE_ATTR.format(
                    name="裂空掌",
                    value=min(lim.max_initial, max(4, round(lim.cap * 0.8))),
                ),
            )

        if (
            len(me.attributes) < lim.max_attrs
            and _best_attr(me, "defense") is None
            and roll > 0.85
        ):
            return Declaration(
                me.player_id,
                CREATE_ATTR.format(
                    name="金刚护盾",
                    value=min(lim.max_initial, max(3, round(lim.cap * 0.6))),
                ),
            )

        dmg = lim.hard
        template = INFERNO_FINISH if target.hp <= dmg else self.rng.choice(INFERNO_ATTACK)
        return Declaration(
            me.player_id,
            template.format(target=target.name, dmg=dmg),
        )
