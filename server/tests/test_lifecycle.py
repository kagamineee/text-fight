"""房间会话回归：使用实际 dispatch、Hub 和房间管理器，不连接外部服务。"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from server import app as api
from server.net.hub import Hub
from server.net.protocol import ProtocolError, parse_inbound
from server.game.models import Phase
from server.rooms import IDLE_ROOM_SECONDS, RoomManager


class Socket:
    client = type("Client", (), {"host": "127.0.0.1"})()

    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message.copy())


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"YAP_PACKS_DIR": tmp.name, "YAP_DEBUG_LOG": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.hub = Hub()
        self.manager = RoomManager(self.hub)
        for name, value in (("hub", self.hub), ("manager", self.manager), ("sessions", {})):
            patcher = patch.object(api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def send(self, conn, **message):
        await api.dispatch(conn, parse_inbound(json.dumps(message)))

    async def hello(self, name):
        conn = api.Conn(Socket())
        await self.send(conn, type="hello", name=name)
        return conn

    async def room(self):
        host = await self.hello("房主")
        await self.send(host, type="create_room", password="test123")
        return host, api.room_of(host)

    async def guest(self, room):
        guest = await self.hello("访客")
        await self.send(guest, type="join_room", code=room.code, password="test123")
        return guest

    async def test_leave_then_create_receives_broadcasts(self):
        conn, old = await self.room()
        await self.send(conn, type="leave_room")
        await self.send(conn, type="create_room", password="test123")
        room = api.room_of(conn)
        await room._broadcast({"type": "probe"})
        self.assertEqual(conn.ws.messages[-1]["type"], "probe")
        self.assertNotIn(conn.player_id, self.hub.members(old.room_id))
        self.assertIn(conn.player_id, self.hub.members(room.room_id))

    async def test_lobby_disconnect_reconnect_keeps_seat_and_name(self):
        host, room = await self.room()
        pid = host.player_id
        await api.on_disconnect(host)
        self.assertIn(pid, room.players)
        self.assertFalse(room.players[pid].connected)
        new = api.Conn(Socket())
        await self.send(new, type="reconnect", token=host.token)
        self.assertEqual(new.nickname, "房主")
        self.assertTrue(room.players[pid].connected)
        self.assertEqual(len(room.players), 1)
        self.assertTrue(any(m["type"] == "snapshot" for m in new.ws.messages))

    async def test_old_socket_cannot_submit_or_disconnect_replacement(self):
        old, room = await self.room()
        new = api.Conn(Socket())
        await self.send(new, type="reconnect", token=old.token)
        await api.on_disconnect(old)
        self.assertTrue(room.players[new.player_id].connected)
        await self.send(old, type="leave_room")
        self.assertEqual(old.ws.messages[-1]["code"], "AUTH_FAILED")
        self.assertIn(new.player_id, room.players)

    async def test_host_leave_transfers_control(self):
        host, room = await self.room()
        guest = await self.guest(room)
        await self.send(host, type="leave_room")
        self.assertEqual(room.host_id, guest.player_id)
        await self.send(guest, type="add_bot", difficulty="normal")
        self.assertEqual(len(room.players), 2)

    async def test_host_disconnect_transfers_without_losing_seat(self):
        host, room = await self.room()
        guest = await self.guest(room)
        await api.on_disconnect(host)
        self.assertEqual(room.host_id, guest.player_id)
        self.assertIn(host.player_id, room.players)

    async def test_kick_cannot_restore_seat_and_can_join_other_room(self):
        host, room = await self.room()
        guest = await self.guest(room)
        await self.send(host, type="kick", player_id=guest.player_id)
        self.assertIsNone(api.sessions[guest.token]["room_id"])
        new = api.Conn(Socket())
        await self.send(new, type="reconnect", token=guest.token)
        self.assertNotIn(guest.player_id, room.players)
        self.assertNotIn(guest.player_id, self.hub.members(room.room_id))
        await self.send(new, type="create_room", password="test123")
        other = api.room_of(new)
        await other._broadcast({"type": "probe"})
        self.assertEqual(new.ws.messages[-1]["type"], "probe")

    async def test_kicked_existing_socket_can_create_room(self):
        host, room = await self.room()
        guest = await self.guest(room)
        await self.send(host, type="kick", player_id=guest.player_id)
        await self.send(guest, type="create_room", password="test123")
        self.assertNotEqual(guest.room_id, room.room_id)

    async def test_grace_expiration_removes_seat_and_empty_room(self):
        host, room = await self.room()
        await api.on_disconnect(host)
        stamp = api.sessions[host.token]["last_seen"]
        await api.cleanup_once(stamp + api.LOBBY_RECONNECT_SECONDS - 1)
        self.assertIn(host.player_id, room.players)
        await api.cleanup_once(stamp + api.LOBBY_RECONNECT_SECONDS + 1)
        self.assertNotIn(room.room_id, self.manager.rooms)
        self.assertIsNone(api.sessions[host.token]["room_id"])
        await api.cleanup_once(stamp + api.SESSION_TTL + 1)
        self.assertNotIn(host.token, api.sessions)

    async def test_idle_bot_lobby_and_disconnected_game_are_reclaimed(self):
        host, room = await self.room()
        await self.send(host, type="add_bot", difficulty="normal")
        await self.send(host, type="leave_room")
        now = time.time()
        await self.manager.cleanup_once(now)
        await self.manager.cleanup_once(now + IDLE_ROOM_SECONDS + 1)
        self.assertNotIn(room.room_id, self.manager.rooms)

    async def test_disconnected_game_is_reclaimed_and_session_revoked(self):
        host, room = await self.room()
        room.phase = Phase.DECLARE
        await api.on_disconnect(host)
        now = time.time()
        await api.cleanup_once(now)
        self.assertIn(host.player_id, room.players)
        await api.cleanup_once(now + IDLE_ROOM_SECONDS + 1)
        self.assertNotIn(room.room_id, self.manager.rooms)
        self.assertNotIn(host.token, api.sessions)

    async def test_reconnect_after_grace_does_not_restore_expired_seat(self):
        host, room = await self.room()
        await api.on_disconnect(host)
        api.sessions[host.token]["last_seen"] = time.time() - api.LOBBY_RECONNECT_SECONDS - 1
        new = api.Conn(Socket())
        await self.send(new, type="reconnect", token=host.token)
        self.assertIsNone(new.room_id)
        self.assertNotIn(host.player_id, room.players)
        self.assertFalse(any(m["type"] == "snapshot" for m in new.ws.messages))

    async def test_leave_finished_game_keeps_result_for_remaining_players(self):
        host, room = await self.room()
        guest = await self.guest(room)
        await room._finish([room.players[host.player_id]])
        await self.send(host, type="leave_room")
        self.assertEqual(room.result_view()["winner_name"], "房主")
        self.assertEqual(room.host_id, guest.player_id)
        await self.send(guest, type="return_lobby")
        self.assertNotIn(host.player_id, room.players)

    async def test_lobby_reset_removes_disconnected_seats(self):
        host, room = await self.room()
        guest = await self.guest(room)
        room.phase = Phase.DECLARE
        await self.send(guest, type="leave_room")
        await room._finish([room.players[host.player_id]])
        await self.send(host, type="return_lobby")
        self.assertEqual(list(room.players), [host.player_id])

    async def test_old_game_private_messages_do_not_leak_into_new_room(self):
        host, old = await self.room()
        old.phase = Phase.DECLARE
        await self.send(host, type="leave_room")
        await self.send(host, type="create_room", password="test123")
        count = len(host.ws.messages)
        await old._send(host.player_id, {"type": "old_private"})
        self.assertEqual(len(host.ws.messages), count)

    async def test_finished_timer_resets_for_new_game(self):
        host, room = await self.room()
        await room._finish([room.players[host.player_id]])
        room._over_since = 1
        await room.reset_for_lobby()
        self.assertIsNone(room._over_since)
        await room._finish([room.players[host.player_id]])
        await self.manager.cleanup_once(time.time() + 100)
        self.assertIn(room.room_id, self.manager.rooms)

    async def test_bot_names_remain_unique_after_removal(self):
        host, room = await self.room()
        first = self.manager.add_bot(room, host.player_id)
        self.manager.add_bot(room, host.player_id)
        self.manager.remove_player(room, host.player_id, first.player_id)
        self.manager.add_bot(room, host.player_id)
        self.assertEqual(len({p.name for p in room.players.values()}), len(room.players))

    async def test_no_room_session_expires_but_connected_one_does_not(self):
        conn = await self.hello("访客")
        api.sessions[conn.token]["last_seen"] = 1
        await api.cleanup_once(time.time())
        self.assertIn(conn.token, api.sessions)
        await api.on_disconnect(conn)
        await api.cleanup_once(time.time() + api.SESSION_TTL + 1)
        self.assertNotIn(conn.token, api.sessions)

    def test_manual_attribute_protocol_is_removed(self):
        with self.assertRaises(ProtocolError):
            parse_inbound(json.dumps({"type": "open_attribute", "name": "作弊武器"}))
