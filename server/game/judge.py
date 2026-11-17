"""P0 确定性裁判。

正式版本（P1）由房主提供的模型读全文做判断；本模块是同一接口的兜底实现：
从自由文本里读出「对谁」「做什么」「多大量级」，产出 effects 与 attr_changes。

铁律：任何数值都要经过 formulas 的钳制层，本模块自己也不例外。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .formulas import IntensityRules, clamp_to_hard
from .models import Declaration, Player

DAMAGE_WORDS = (
    "打", "揍", "轰", "砍", "劈", "刺", "戳", "踢", "踹", "撞", "摔", "扔", "砸",
    "烧", "冻", "电", "毒", "咬", "掐", "勒", "爆", "炸", "撕", "斩", "射", "扫",
    "拍", "抽", "敲", "压", "碾", "吞", "诅咒", "重创", "暴击", "捅",
)

HEAL_WORDS = (
    "治疗", "治愈", "回血", "加血", "补血", "恢复", "疗伤", "愈合", "输血",
    "喂药", "包扎", "急救", "止血", "回一口", "续命",
)

SELF_WORDS = ("我自己", "自己", "自我", "自残", "自爆", "自杀", "撞墙", "原地爆炸", "往自己")

ALL_WORDS = ("所有人", "全体", "全场", "大家", "每个人", "众人")

CREATE_WORDS = (
    "觉醒", "领悟", "凝聚", "练成", "习得", "获得", "召唤", "长出",
    "开出", "形成", "凝成", "化作", "竖起", "撑起",
)

UP_WORDS = ("提升", "增强", "强化", "上涨", "变强", "加厚", "增长", "暴涨", "翻倍", "提升到")
DOWN_WORDS = ("削弱", "降低", "衰减", "下降", "变弱", "削减", "打薄", "废掉", "封印", "打碎")
REMOVE_WORDS = ("废掉", "舍弃", "忘掉", "删除", "不要了", "丢掉", "打碎", "封印")

DEFENSE_HINTS = ("盾", "甲", "护", "防", "壳", "壁", "罩", "铁布衫", "龟息")
ATTACK_HINTS = ("拳", "剑", "刀", "爪", "牙", "火", "雷", "冰", "毒", "掌", "枪")

QUOTE_PATTERNS = (
    re.compile(r"「([^」]{1,16})」"),
    re.compile(r"“([^”]{1,16})”"),
    re.compile(r"《([^》]{1,16})》"),
)
CREATE_PATTERN = re.compile(
    r"(?:觉醒|领悟|凝聚|练成|习得|获得|召唤|长出|开出|形成|凝成|化作|竖起|撑起)"
    r"(?:了|出|成|起|一个|一道|一层)?\s*[「“《]?([\u4e00-\u9fa5A-Za-z0-9]{1,10})"
)
NUMBER_PATTERN = re.compile(r"(\d{1,3})\s*(?:点|滴|层|分)")


@dataclass
class Verdict:
    effects: list[dict] = field(default_factory=list)
    attr_changes: list[dict] = field(default_factory=list)
    narrative: str = ""


def _alive_others(players: dict[str, Player], actor_id: str) -> list[Player]:
    return [p for p in players.values() if p.alive and p.player_id != actor_id]


def _magnitude(text: str, pack, turn_no: int) -> float:
    rules = IntensityRules(
        cap_base=pack.intensity.cap_base,
        cap_growth=pack.intensity.cap_growth,
        ceiling_factor=pack.intensity.ceiling_factor,
        level_words=pack.intensity.level_words,
    )
    cap = rules.cap(turn_no)
    match = NUMBER_PATTERN.search(text)
    if match:
        value = float(match.group(1))
    else:
        value = 0.0
        for word, ratio in pack.intensity.level_words.items():
            if word in text:
                value = cap * ratio
                break
        if value == 0.0:
            value = cap * 0.6
    return clamp_to_hard(value, turn_no, rules)


def _quoted_name(text: str) -> str:
    for pattern in QUOTE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    match = CREATE_PATTERN.search(text)
    if match:
        name = match.group(1)
        for prefix in ("一个", "一道", "一层"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name
    return ""


def _attr_type(name: str, text: str) -> str:
    if any(w in name for w in DEFENSE_HINTS) or any(w in text for w in ("防御", "护住")):
        return "defense"
    if any(w in name for w in ATTACK_HINTS):
        return "attack"
    return "utility"


def _existing_attr(players: dict[str, Player], name: str):
    for player in players.values():
        for attr in player.attributes.values():
            if attr.name == name:
                return player, attr
    return None, None


def adjudicate(
    players: dict[str, Player],
    turn_no: int,
    pack,
    decl: Declaration,
) -> Verdict:
    actor = players[decl.actor_id]
    text = decl.content or ""
    others = _alive_others(players, actor.player_id)

    wants_heal = any(w in text for w in HEAL_WORDS)
    wants_damage = any(w in text for w in DAMAGE_WORDS)
    mentions_self = any(w in text for w in SELF_WORDS)
    mentions_all = any(w in text for w in ALL_WORDS)

    named = [p for p in others if p.name and p.name in text]

    targets: list[Player] = []
    if named:
        targets = named
    elif mentions_all:
        targets = others
    elif mentions_self:
        targets = [actor]
    elif wants_heal:
        targets = [actor]
    elif wants_damage and others:
        targets = [min(others, key=lambda p: (p.hp, p.seat))]

    amount = int(round(_magnitude(text, pack, turn_no)))
    if wants_damage:
        amount = max(1, amount) if targets else 0

    result = Verdict()
    sentences: list[str] = []

    if targets and wants_damage and not wants_heal:
        for target in targets:
            actual = min(amount, target.hp)
            if actual <= 0:
                continue
            result.effects.append({"player_id": target.player_id, "hp_delta": -actual})
            if target.player_id == actor.player_id:
                sentences.append(f"{actor.name} 把自己弄掉了 {actual} 点血。")
            else:
                sentences.append(f"{actor.name} 让 {target.name} 掉了 {actual} 点血。")
    elif targets and wants_heal:
        for target in targets:
            actual = min(amount, target.hp_max - target.hp)
            if actual <= 0:
                sentences.append(f"{target.name} 血是满的，回不进去。")
                continue
            result.effects.append({"player_id": target.player_id, "hp_delta": actual})
            if target.player_id == actor.player_id:
                sentences.append(f"{actor.name} 给自己回了 {actual} 点血。")
            else:
                sentences.append(f"{actor.name} 给 {target.name} 回了 {actual} 点血。")

    attr_name = _quoted_name(text)
    if attr_name and any(w in text for w in CREATE_WORDS):
        owner, existing = _existing_attr(players, attr_name)
        if existing is None and len(actor.attributes) < pack.attributes.max_count:
            attr_type = _attr_type(attr_name, text)
            value = max(1, min(amount, pack.attributes.max_initial))
            result.attr_changes.append(
                {
                    "owner": actor.player_id,
                    "name": attr_name,
                    "attr_type": attr_type,
                    "value": value,
                    "cap": min(max(value * 2, value), pack.attributes.max_cap),
                    "narrative": text[:60],
                    "created": True,
                }
            )
            sentences.append(f"{actor.name} 多了个属性「{attr_name}」。")

    for player in players.values():
        for attr in list(player.attributes.values()):
            if attr.name not in text:
                continue
            if any(w in text for w in REMOVE_WORDS):
                result.attr_changes.append(
                    {
                        "owner": player.player_id,
                        "attr_id": attr.attr_id,
                        "name": attr.name,
                        "remove": True,
                    }
                )
                sentences.append(f"{player.name} 的「{attr.name}」没了。")
                continue
            delta = 0
            if any(w in text for w in UP_WORDS):
                delta = 2
            elif any(w in text for w in DOWN_WORDS):
                delta = -2
            if delta == 0:
                continue
            after = max(0, min(attr.cap, attr.value + delta))
            if after == attr.value:
                continue
            result.attr_changes.append(
                {
                    "owner": player.player_id,
                    "attr_id": attr.attr_id,
                    "name": attr.name,
                    "value": after,
                    "cap": attr.cap,
                    "created": False,
                }
            )
            sentences.append(f"{player.name} 的「{attr.name}」现在是 {after}。")

    if not sentences:
        sentences.append(f"{actor.name} 说了一通，场上没人掉血。")

    result.narrative = "".join(sentences)
    return result
