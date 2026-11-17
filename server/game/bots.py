"""假人：模板发言 + 种子化随机。

P0 没有真模型，假人只负责把核心循环跑起来。
它们写的是自由文本，和真人一样交给裁判读。
"""

from __future__ import annotations

import random

from .models import Declaration, Player

ATTACK_TEMPLATES = [
    "我一拳砸在{target}的肩上。",
    "我抓住{target}的衣领，把他往墙上一推。",
    "我抬腿踹向{target}的膝盖。",
    "我拎起椅子，朝{target}扔过去。",
    "我照着{target}的肚子来了一下。",
    "我冲过去，用肩膀撞开{target}。",
    "我踩住{target}的脚背，顺手给了他一下。",
    "我拿旁边的杯子砸向{target}的额头。",
]

GUARD_TEMPLATES = [
    "我往后退了两步，先把气缓过来。",
    "我抬起胳膊护住头，没急着还手。",
    "我把椅子挡在身前，盯着对面。",
]

HEAL_TEMPLATES = [
    "我撕了截袖子，把{target}的伤口按住。",
    "我灌了{target}半壶水，让他缓一缓。",
    "我往{target}嘴里塞了颗药。",
]

SELF_HARM_TEMPLATES = [
    "我抬手打了自己一巴掌。",
    "我一头撞在柱子上，眼前发黑。",
    "我拿杯子砸了自己手背一下。",
]

class BotBrain:
    def __init__(self, seed: int, preset_index: int = 0):
        self.rng = random.Random(seed)
        self.preset_index = preset_index

    def _alive_enemies(self, me: Player, players: dict[str, Player]) -> list[Player]:
        return [
            p for p in players.values() if p.alive and p.player_id != me.player_id
        ]

    def declare(
        self, me: Player, players: dict[str, Player], turn_no: int
    ) -> Declaration:
        enemies = self._alive_enemies(me, players)
        if not enemies:
            return Declaration(me.player_id, "场上没别人了。")
        target = min(enemies, key=lambda p: (p.hp, self.rng.random()))
        roll = self.rng.random()
        hp_ratio = me.hp / max(1, me.hp_max)

        if hp_ratio < 0.35 and roll < 0.45:
            return Declaration(
                me.player_id,
                self.rng.choice(HEAL_TEMPLATES).format(target="自己"),
            )

        if roll < 0.12:
            return Declaration(
                me.player_id,
                self.rng.choice(SELF_HARM_TEMPLATES),
            )

        if roll < 0.28:
            return Declaration(me.player_id, self.rng.choice(GUARD_TEMPLATES))

        return Declaration(
            me.player_id,
            self.rng.choice(ATTACK_TEMPLATES).format(target=target.name),
        )
