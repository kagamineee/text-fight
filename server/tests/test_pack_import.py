"""扩展包导入：真实 ASGI 请求、校验、冲突确认和热加载。"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import app
from server.rooms import RoomManager
from server.tests.test_packs import NullHub, pack_yaml


class PackImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"YAP_PACKS_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.manager = RoomManager(NullHub())
        manager_patch = patch("server.app.manager", self.manager)
        manager_patch.start()
        self.addCleanup(manager_patch.stop)

    def request(self, body, host="127.0.0.1", path="/api/packs/import"):
        async def run():
            messages = []
            raw = json.dumps(body).encode("utf-8")
            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}
            async def send(message):
                messages.append(message)
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": (host, 12345), "server": ("localhost", 8000), "root_path": "",
            }
            await app(scope, receive, send)
            self.assertEqual(messages[0]["status"], 200)
            return json.loads(b"".join(m.get("body", b"") for m in messages))
        return asyncio.run(run())

    def test_import_hot_loads_and_can_create_room(self):
        result = self.request({"yaml": "\ufeff" + pack_yaml()})
        self.assertTrue(result["ok"])
        self.assertTrue((Path(self.tmp.name) / "my-pack.yaml").is_file())
        room, _ = self.manager.create_room("甲", "pass", pack_id="my-pack")
        self.assertEqual(room.pack.id, "my-pack")

    def test_invalid_does_not_write(self):
        result = self.request({"yaml": pack_yaml(hp_base=1)})
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_conflict_requires_confirmation_and_does_not_change_running_room(self):
        self.request({"yaml": pack_yaml()})
        room, _ = self.manager.create_room("甲", "pass", pack_id="my-pack")
        result = self.request({"yaml": pack_yaml(hp_base=120)})
        self.assertTrue(result["conflict"])
        self.assertEqual(self.manager.packs["my-pack"].hp.base, 100)
        result = self.request({"yaml": pack_yaml(hp_base=120), "overwrite": True})
        self.assertTrue(result["ok"])
        self.assertEqual(self.manager.packs["my-pack"].hp.base, 120)
        self.assertEqual(room.pack.hp.base, 100)

    def test_remote_import_denied(self):
        self.assertFalse(self.request({"yaml": pack_yaml()}, host="100.64.0.2")["ok"])
        self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_overwrite_pack_whose_filename_differs_from_id(self):
        path = Path(self.tmp.name) / "zzz-custom-filename.yaml"
        path.write_text(pack_yaml(), encoding="utf-8")
        self.manager.reload_packs()
        result = self.request({"yaml": pack_yaml(hp_base=130), "overwrite": True})
        self.assertTrue(result["ok"])
        self.assertEqual(self.manager.packs["my-pack"].hp.base, 130)
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)

    def test_oversized_and_bad_payload_rejected(self):
        for body in [{"yaml": "a" * (256 * 1024 + 1)}, [], {"yaml": 123},
                     {"yaml": pack_yaml("../escape")}]:
            self.assertFalse(self.request(body)["ok"])
        self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_size_limit_shared_by_validate_import_save_and_loader(self):
        text = pack_yaml() + "\n#" + "x" * (210 * 1024)
        for path in ("/api/packs/import", "/api/dev/save", "/api/dev/validate"):
            result = self.request({"yaml": text}, path=path)
            self.assertFalse(result["ok"])
            self.assertIn("200", result["errors"][0])
        self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_exact_size_limit_import_remains_loadable(self):
        from server.packs.loader import MAX_PACK_BYTES
        text = pack_yaml() + "\n#"
        text += "x" * (MAX_PACK_BYTES - len(text.encode("utf-8")))
        result = self.request({"yaml": text})
        self.assertTrue(result["ok"])
        self.assertIn("my-pack", self.manager.packs)
        self.assertEqual((Path(self.tmp.name) / "my-pack.yaml").stat().st_size, MAX_PACK_BYTES)

    def test_dev_save_updates_registered_filename(self):
        path = Path(self.tmp.name) / "zzz-custom.yaml"
        path.write_text(pack_yaml(), encoding="utf-8")
        self.manager.reload_packs()
        result = self.request({"yaml": pack_yaml(hp_base=130)}, path="/api/dev/save")
        self.assertTrue(result["ok"])
        self.assertEqual(self.manager.packs["my-pack"].hp.base, 130)
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
