"""离线仅识别有限、完整匹配的模板语法；未识别原文不猜测机制。"""
import re

from . import bots
from .effect_plan import DeclarationPlan, Requirement, EffectSpec, Action, Lifetime, Revision, validate_plan


def parse_offline(declaration, actor_id, players, active_effects=()):
    templates = (bots.EASY_ATTACK + bots.NORMAL_ATTACK + bots.HARD_ATTACK
                 + bots.INFERNO_ATTACK + [bots.HARD_ATTR_ATTACK, bots.INFERNO_FINISH])
    heals = [bots.EASY_HEAL, bots.NORMAL_HEAL, bots.HARD_HEAL, bots.INFERNO_HEAL]
    for kind, candidates in (("damage", templates), ("heal", heals)):
        for template in candidates:
            pattern = re.escape(template)
            for key, regex in (("target", r"(?P<target>.+?)"), ("dmg", r"(?P<amount>\d+)"),
                               ("n", r"(?P<amount>\d+)"), ("attr", r"(?P<attr>[^」]+)")):
                pattern = pattern.replace(re.escape("{" + key + "}"), regex)
            match = re.fullmatch(pattern, declaration.strip())
            if not match:
                continue
            targets = [actor_id] if kind == "heal" else [p.player_id for p in players.values()
                if p.name == match.group("target") and p.alive]
            target = targets[0] if len(targets) == 1 else None
            if not target or int(match.group("amount")) < 1 or int(match.group("amount")) > 9999:
                continue
            return DeclarationPlan(requirements=[Requirement(quote=declaration, status="supported", effects=[
                EffectSpec(name="模板行动", targets=[target], trigger="immediate", lifetime=Lifetime(unit="instant"),
                           actions=[Action(kind=kind, amount=int(match.group("amount")))])])])
    effects = {item["effect_id"]: item for item in active_effects}
    fields = {"name": r"[^」]+", "attr": r"[^」]+", "target": r".+?",
              "value": r"\d+", "turns": r"\d+", "dmg": r"\d+", "effect_id": r"[^）]+"}
    for template in (bots.CREATE_ARMOR, bots.FREEZE_ATTR, bots.UPGRADE_ARMOR, bots.DISPEL_ATTACK):
        pattern = re.escape(template)
        for key, regex in fields.items():
            pattern = pattern.replace(re.escape("{" + key + "}"), f"(?P<{key}>{regex})")
        match = re.fullmatch(pattern, declaration.strip())
        if not match:
            continue
        data = match.groupdict()
        target = actor_id
        if "target" in data:
            found = [p.player_id for p in players.values() if p.name == data["target"] and p.alive]
            if len(found) != 1:
                break
            target = found[0]
        try:
            req = None
            if template in (bots.CREATE_ARMOR, bots.FREEZE_ATTR):
                armor = template == bots.CREATE_ARMOR
                spec = EffectSpec(name=data["name"] if armor else "冰封咒", targets=[target],
                    trigger="passive", lifetime=Lifetime(unit="global_turn", count=int(data["turns"])),
                    actions=[Action(kind="armor", amount=int(data["value"])) if armor else Action(kind="skip_action")])
                req = Requirement(quote=declaration, status="supported", effects=[spec])
            elif template == bots.UPGRADE_ARMOR:
                item = effects.get(data["effect_id"])
                if item is None:
                    break
                spec = EffectSpec.model_validate(item["spec"])
                armor = next((a for a in spec.actions if a.kind == "armor"), None)
                if (spec.name != data["attr"] or spec.targets != [actor_id]
                        or spec.protected or armor is None or int(data["value"]) <= armor.amount):
                    break
                armor.amount = int(data["value"])
                spec = EffectSpec.model_validate(spec.model_dump())
                req = Requirement(quote=declaration, status="supported", revisions=[
                    Revision(effect_id=data["effect_id"], replacement=spec)])
            else:
                item = effects.get(data["effect_id"])
                attr = players[target].attributes.get(data["effect_id"])
                if item is not None:
                    spec = EffectSpec.model_validate(item["spec"])
                    valid = target in spec.targets and spec.name == data["attr"]
                else:
                    valid = attr is not None and attr.name == data["attr"]
                if not valid:
                    break
                req = Requirement(quote=declaration, status="supported", effects=[EffectSpec(
                    name="解除并攻击", targets=[target], trigger="immediate", lifetime=Lifetime(unit="instant"),
                    actions=[Action(kind="remove_effect", effect_id=data["effect_id"]),
                             Action(kind="damage", amount=int(data["dmg"]))])])
            candidate = DeclarationPlan(requirements=[req])
            ids = set(effects) | {a.attr_id for p in players.values() for a in p.attributes.values()}
            validate_plan(candidate, declaration, players, ids)
            return candidate
        except ValueError:
            break
    return DeclarationPlan(requirements=[Requirement(quote=declaration, status="unsupported",
        reason="裁判不可用；离线仅执行完整匹配的攻击、治疗、定时护甲、冻结、护甲强化和显式解除模板，本次宣言未被近似替换。")])
