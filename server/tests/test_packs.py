"""扩展包注册、校验与选包测试。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.packs.loader import PackError, PackRegistry, load_pack_file
from server.rooms import RoomError, RoomManager


class NullHub:
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


def pack_yaml(pack_id: str = "my-pack", hp_base: int = 100) -> str:
    return f"""id: {pack_id}
name: 测试包
description: 单元测试用
hp:
  base: {hp_base}
  regen: 5
intensity:
  cap_base: 8
  cap_growth: 2
  ceiling_factor: 1.5
  level_words:
    寻常: 0.6
attributes:
  max_count: 10
  max_initial: 10
  max_cap: 30
  max_exchange_rate: 2.0
timers:
  declare_s: 60
  judge_s: 60
disaster:
  start_turn: 15
  every_n_turns: 3
  base_damage: 2
  growth: 1
appeal:
  count: 2
  window_s: 30
  vote_s: 60
inheritance:
  power_ratio: 0.4
"""


class PackRegistryTest(unittest.TestCase):
    def test_builtin_packs_loaded(self) -> None:
        registry = PackRegistry().reload()
        self.assertEqual(registry.issues, [])
        self.assertEqual(
            set(registry.packs),
            {"default-arena", "cyber-wasteland", "xiuzhen-brawl"},
        )
        self.assertEqual(registry.default_id, "default-arena")

    def test_curves_are_distinct(self) -> None:
        packs = PackRegistry().reload().packs
        hp = {p.id: p.hp.base for p in packs.values()}
        self.assertEqual(len(set(hp.values())), len(hp))
        self.assertLess(
            packs["cyber-wasteland"].disaster.start_turn,
            packs["default-arena"].disaster.start_turn,
        )
        self.assertGreater(
            packs["xiuzhen-brawl"].disaster.start_turn,
            packs["default-arena"].disaster.start_turn,
        )

    def test_user_pack_override_and_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            (user_dir / "override.yaml").write_text(
                pack_yaml("default-arena", hp_base=120), encoding="utf-8"
            )
            with patch.dict(os.environ, {"YAP_PACKS_DIR": str(user_dir)}):
                registry = PackRegistry().reload()
                self.assertEqual(registry.packs["default-arena"].hp.base, 120)
                self.assertFalse(registry.maybe_reload())

                (user_dir / "extra.yaml").write_text(pack_yaml("extra-pack"), encoding="utf-8")
                self.assertTrue(registry.maybe_reload())
                self.assertIn("extra-pack", registry.packs)

    def test_invalid_pack_keeps_others_and_reports_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            (user_dir / "broken.yaml").write_text(pack_yaml("broken", hp_base=5), encoding="utf-8")
            with patch.dict(os.environ, {"YAP_PACKS_DIR": str(user_dir)}):
                registry = PackRegistry().reload()
        self.assertIn("default-arena", registry.packs)
        self.assertNotIn("broken", registry.packs)
        self.assertEqual(len(registry.issues), 1)
        message = registry.issues[0]["message"]
        self.assertIn("初始血量", message)
        self.assertIn("不能小于 10", message)

    def test_pack_outside_allowed_roots_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outside.yaml"
            path.write_text(pack_yaml("outside-pack"), encoding="utf-8")
            with self.assertRaises(PackError) as ctx:
                load_pack_file(path)
        self.assertIn("越界", str(ctx.exception))


class RoomPackSelectionTest(unittest.TestCase):
    def test_create_room_with_pack(self) -> None:
        manager = RoomManager(NullHub())
        room, _host = manager.create_room("甲", "test-pass", pack_id="cyber-wasteland")
        self.assertEqual(room.pack.id, "cyber-wasteland")
        self.assertEqual(room.room_state()["pack"]["id"], "cyber-wasteland")

    def test_default_pack(self) -> None:
        manager = RoomManager(NullHub())
        room, _host = manager.create_room("甲", "test-pass")
        self.assertEqual(room.pack.id, "default-arena")

    def test_unknown_pack(self) -> None:
        manager = RoomManager(NullHub())
        with self.assertRaises(RoomError) as ctx:
            manager.create_room("甲", "test-pass", pack_id="missing")
        self.assertEqual(ctx.exception.code, "PACK_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
