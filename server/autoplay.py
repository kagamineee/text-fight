"""无界面自动对局：P0 的核心验收工具。

用法：
    python -m server.autoplay --games 20 --bots 4

它不启动 HTTP 服务，直接在进程内驱动引擎跑完整局，
用于验证：HP 不为负、回合轮转正确、事件序号连续、结算不抛异常。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .game import engine
from .game.bots import BotBrain
from .rooms import RoomManager


class NullHub:
    """自动局不需要真实连接。"""

    async def send_player(self, *args, **kwargs) -> None:
        return None

    async def broadcast_room(self, *args, **kwargs) -> None:
        return None

    async def bind(self, *args, **kwargs) -> None:
        return None

    async def unbind(self, *args, **kwargs) -> None:
        return None

    async def move(self, *args, **kwargs) -> None:
        return None

    async def drop_room(self, *args, **kwargs) -> None:
        return None


async def run_one(bots: int, index: int, pack_id: str = "") -> dict:
    manager = RoomManager(NullHub())
    room, host = manager.create_room(f"观察者{index}", "test-pass", pack_id=pack_id or None)
    # 房主也是人类席位，自动局里让它同样由假人脑驱动
    room.bot_brains[host.player_id] = BotBrain(seed=0xB0B)
    host.ready = True
    for _ in range(bots):
        manager.add_bot(room, host.player_id)
    manager.start_game(room, host.player_id)

    try:
        async with asyncio.timeout(180):
            while not room.over:
                await asyncio.sleep(0.02)
    finally:
        await room.stop()

    seqs = [e.seq for e in room.ledger]
    if seqs != list(range(1, len(seqs) + 1)):
        raise AssertionError(f"事件序号不连续：{seqs[:5]}...{seqs[-3:]}")
    for p in room.players.values():
        if not 0 <= p.hp <= p.hp_max or p.alive != (p.hp > 0):
            raise AssertionError(f"{p.name} 的生命状态不一致：HP={p.hp}, alive={p.alive}")
    if room.end_reason:
        raise AssertionError(room.end_reason)
    if room.turn_no < 1:
        raise AssertionError("对局未产生任何回合")

    alive = [p for p in room.players.values() if p.alive]
    disasters = sum(1 for e in room.ledger if e.type == "disaster")
    specials = sum(1 for e in room.ledger if e.type == "event")
    inherited = sum(
        1
        for e in room.ledger
        if e.type == "inheritance" and (e.payload or {}).get("granted")
    )
    if room.turn_no >= room.pack.disaster.start_turn and not disasters:
        raise AssertionError("已过天灾起始回合却没有天灾事件")
    return {
        "pack": room.pack.name,
        "turns": room.turn_no,
        "winner": room.players[room.winner].name if room.winner else "无",
        "alive": len(alive),
        "events": len(room.ledger),
        "disasters": disasters,
        "specials": specials,
        "inherited": inherited,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="文字角斗场 · 自动对局")
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--bots", type=int, default=4)
    parser.add_argument("--pack", default="", help="扩展包 id，留空用默认包")
    args = parser.parse_args()

    # 自动局只验证引擎，默认不调用裁判/假人 AI
    os.environ.setdefault("YAP_JUDGE_AI", "0")
    os.environ.setdefault("YAP_BOT_AI", "0")

    # 自动局不需要"思考时间"
    engine.BOT_THINK_MIN = 0.0
    engine.BOT_THINK_MAX = 0.0

    failures = 0
    for i in range(args.games):
        try:
            result = await run_one(args.bots, i, args.pack)
            print(
                f"[{i + 1}/{args.games}] {result['pack']} | 回合 {result['turns']:>3} | "
                f"事件 {result['events']:>5} | 天灾 {result['disasters']:>2} | "
                f"特殊 {result['specials']:>2} | 继承 {result['inherited']:>2} | 幸存 {result['alive']} | "
                f"胜者 {result['winner']}"
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[{i + 1}/{args.games}] 失败：{exc}")

    print(f"\n完成：{args.games - failures}/{args.games} 局通过")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
