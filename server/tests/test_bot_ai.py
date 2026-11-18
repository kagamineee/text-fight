"""假人 AI 单测：困难/炼狱走房主代理，失败/超时回落模板。"""

import asyncio
import os
import unittest
from unittest.mock import patch

import server.game.engine as engine_module
from server.game import judge_ai
from server.game.engine import Room
from server.game.models import Phase
from server.packs.loader import load_packs
from server.tests.test_events import NullHub


def make_room(difficulty: str = "hard", n_players: int = 2):
    hub = NullHub()
    room = Room(
        room_id="r1",
        code="AAAAAA",
        password_hash="",
        pack=load_packs()["default-arena"],
        host_id="",
        hub=hub,
    )
    for i in range(n_players):
        room.add_player(["甲", "乙"][i], kind="human", pid=f"p{i}")
    bot = room.add_player("假人", kind="bot", pid="bot", difficulty=difficulty)
    brain = room.bot_brains["bot"]
    room.phase = Phase.DECLARE
    room.current_actor = bot.player_id
    room.turn_no = 4
    return room, bot, brain


class BotDraftTest(unittest.TestCase):
    def test_disabled_returns_none(self) -> None:
        room, bot, _ = make_room()
        calls = {"n": 0}

        async def fake_chat(**kwargs):
            calls["n"] += 1
            return "不该被调用"

        with patch.dict(os.environ, {"YAP_BOT_AI": "0"}):
            with patch("server.game.judge_ai.chat_once", new=fake_chat):
                text = asyncio.run(
                    judge_ai.draft_bot_action(
                        bot=bot,
                        players=room.players,
                        turn_no=room.turn_no,
                        pack=room.pack,
                        ledger=[],
                    )
                )
        self.assertIsNone(text)
        self.assertEqual(calls["n"], 0)

    def test_agent_down_returns_none(self) -> None:
        room, bot, _ = make_room()
        calls = {"n": 0}

        async def fake_health(*args, **kwargs):
            return None

        async def fake_chat(**kwargs):
            calls["n"] += 1
            return "不该被调用"

        with patch("server.game.judge_ai.health", new=fake_health):
            with patch("server.game.judge_ai.chat_once", new=fake_chat):
                text = asyncio.run(
                    judge_ai.draft_bot_action(
                        bot=bot,
                        players=room.players,
                        turn_no=room.turn_no,
                        pack=room.pack,
                        ledger=[],
                    )
                )
        self.assertIsNone(text)
        self.assertEqual(calls["n"], 0)

    def test_output_is_cleaned(self) -> None:
        room, bot, _ = make_room()

        async def fake_health(*args, **kwargs):
            return {"ok": True}

        async def fake_chat(**kwargs):
            return "```\n行动：我一脚踹向乙的膝盖。\n```"

        with patch("server.game.judge_ai.health", new=fake_health):
            with patch("server.game.judge_ai.chat_once", new=fake_chat):
                text = asyncio.run(
                    judge_ai.draft_bot_action(
                        bot=bot,
                        players=room.players,
                        turn_no=room.turn_no,
                        pack=room.pack,
                        ledger=[],
                    )
                )
        self.assertEqual(text, "我一脚踹向乙的膝盖。")

    def test_empty_output_returns_none(self) -> None:
        room, bot, _ = make_room()

        async def fake_health(*args, **kwargs):
            return {"ok": True}

        async def fake_chat(**kwargs):
            return "``` \n ```"

        with patch("server.game.judge_ai.health", new=fake_health):
            with patch("server.game.judge_ai.chat_once", new=fake_chat):
                text = asyncio.run(
                    judge_ai.draft_bot_action(
                        bot=bot,
                        players=room.players,
                        turn_no=room.turn_no,
                        pack=room.pack,
                        ledger=[],
                    )
                )
        self.assertIsNone(text)


class BotDeclareTest(unittest.TestCase):
    def setUp(self) -> None:
        self._think_min = engine_module.BOT_THINK_MIN
        self._think_max = engine_module.BOT_THINK_MAX
        engine_module.BOT_THINK_MIN = 0.0
        engine_module.BOT_THINK_MAX = 0.0

    def tearDown(self) -> None:
        engine_module.BOT_THINK_MIN = self._think_min
        engine_module.BOT_THINK_MAX = self._think_max

    def _capture(self, room):
        captured: dict = {}

        async def fake_submit(pid, decl):
            captured["pid"] = pid
            captured["decl"] = decl

        room._collector.submit = fake_submit
        return captured

    def test_hard_uses_ai_text(self) -> None:
        room, bot, brain = make_room("hard")
        captured = self._capture(room)

        async def fake_draft(**kwargs):
            return "我侧身贴近乙，一肘砸在他的肋骨上。"

        with patch("server.game.judge_ai.draft_bot_action", new=fake_draft):
            asyncio.run(room._bot_declare(bot, brain))

        self.assertEqual(captured["pid"], "bot")
        self.assertEqual(captured["decl"].content, "我侧身贴近乙，一肘砸在他的肋骨上。")

    def test_inferno_uses_ai_text(self) -> None:
        room, bot, brain = make_room("inferno")
        captured = self._capture(room)

        async def fake_draft(**kwargs):
            return "我一掌拍碎乙的护甲，再补上一脚。"

        with patch("server.game.judge_ai.draft_bot_action", new=fake_draft):
            asyncio.run(room._bot_declare(bot, brain))

        self.assertEqual(captured["decl"].content, "我一掌拍碎乙的护甲，再补上一脚。")

    def test_easy_never_calls_ai(self) -> None:
        room, bot, brain = make_room("easy")
        captured = self._capture(room)
        calls = {"n": 0}

        async def fake_draft(**kwargs):
            calls["n"] += 1
            return "不该被调用"

        with patch("server.game.judge_ai.draft_bot_action", new=fake_draft):
            asyncio.run(room._bot_declare(bot, brain))

        self.assertEqual(calls["n"], 0)
        self.assertTrue(captured["decl"].content.strip())

    def test_normal_never_calls_ai(self) -> None:
        room, bot, brain = make_room("normal")
        captured = self._capture(room)
        calls = {"n": 0}

        async def fake_draft(**kwargs):
            calls["n"] += 1
            return "不该被调用"

        with patch("server.game.judge_ai.draft_bot_action", new=fake_draft):
            asyncio.run(room._bot_declare(bot, brain))

        self.assertEqual(calls["n"], 0)
        self.assertTrue(captured["decl"].content.strip())

    def test_hard_falls_back_when_ai_fails(self) -> None:
        room, bot, brain = make_room("hard")
        captured = self._capture(room)

        async def fake_draft(**kwargs):
            return None

        with patch("server.game.judge_ai.draft_bot_action", new=fake_draft):
            asyncio.run(room._bot_declare(bot, brain))

        self.assertTrue(captured["decl"].content.strip())

    def test_late_ai_result_is_discarded(self) -> None:
        room, bot, brain = make_room("hard")
        captured = self._capture(room)

        async def fake_draft(**kwargs):
            room.phase = Phase.ADJUDICATE
            return "迟到的宣言"

        with patch("server.game.judge_ai.draft_bot_action", new=fake_draft):
            asyncio.run(room._bot_declare(bot, brain))

        self.assertEqual(captured, {})


if __name__ == "__main__":
    unittest.main()
