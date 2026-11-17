"""无界面自动对局：P0 的核心验收工具。

用法：
    python -m server.autoplay --games 20 --bots 4

它不启动 HTTP 服务，直接在进程内驱动引擎跑完整局，
用于验证：HP 不为负、回合轮转正确、事件序号连续、结算不抛异常。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

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


async def run_one(data_dir: Path, bots: int, index: int) -> dict:
    manager = RoomManager(data_dir, NullHub())
    room, host = manager.create_room(f"观察者{index}", "test-pass")
    # 房主也是人类席位，自动局里让它同样由假人脑驱动
    room.bot_brains[host.player_id] = BotBrain(seed=0xB0B, preset_index=0)
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
        if p.hp < 0:
            raise AssertionError(f"{p.name} 的 HP 为负：{p.hp}")
    if room.turn_no < 1:
        raise AssertionError("对局未产生任何回合")

    alive = [p for p in room.players.values() if p.alive]
    return {
        "turns": room.turn_no,
        "winner": room.players[room.winner].name if room.winner else "无",
        "alive": len(alive),
        "events": len(room.ledger),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="文字角斗场 · 自动对局")
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--bots", type=int, default=4)
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = parser.parse_args()

    # 自动局不需要"思考时间"
    engine.BOT_THINK_MIN = 0.0
    engine.BOT_THINK_MAX = 0.0

    data_dir = Path(tempfile.mkdtemp(prefix="yap-autoplay-"))
    failures = 0
    try:
        for i in range(args.games):
            try:
                result = await run_one(data_dir / f"g{i}", args.bots, i)
                print(
                    f"[{i + 1}/{args.games}] 回合 {result['turns']:>3} | "
                    f"事件 {result['events']:>5} | 幸存 {result['alive']} | "
                    f"胜者 {result['winner']}"
                )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[{i + 1}/{args.games}] 失败：{exc}")
    finally:
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)

    print(f"\n完成：{args.games - failures}/{args.games} 局通过")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
