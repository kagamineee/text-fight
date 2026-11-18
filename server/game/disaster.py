"""天灾：按扩展包曲线对全场施加递增的环境伤害。

数值完全由服务端结算，不经裁判、无视护甲与免伤；本模块只负责算数与文案。
"""

from __future__ import annotations

from ..packs.loader import DisasterCfg, Pack


def level_for(pack: Pack, turn_no: int) -> int:
    """当前天灾等级：起始回合为 1 级，之后每 every_n_turns 升一级。"""
    cfg = pack.disaster
    if turn_no < cfg.start_turn:
        return 0
    return (turn_no - cfg.start_turn) // cfg.every_n_turns + 1


def damage_for(pack: Pack, level: int) -> int:
    if level <= 0:
        return 0
    cfg = pack.disaster
    return cfg.base_damage + cfg.growth * (level - 1)


def is_new_level(cfg: DisasterCfg, turn_no: int) -> bool:
    """本回合是否恰好是天灾升级的那一回合（用于文案）。"""
    if turn_no < cfg.start_turn:
        return False
    return (turn_no - cfg.start_turn) % cfg.every_n_turns == 0


def narrative(cfg: DisasterCfg, turn_no: int, level: int, damage: int) -> str:
    if is_new_level(cfg, turn_no):
        if level == 1:
            return f"第 {turn_no} 回合，天灾降临：竞技场的天空渗出血色，全场承受 {damage} 点侵蚀。"
        return f"天灾升至 {level} 级：大地开裂，侵蚀加剧，全场承受 {damage} 点伤害。"
    return f"天灾持续：第 {turn_no} 回合，全场再承受 {damage} 点侵蚀。"
