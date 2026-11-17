"""文字角斗场 · 服务端入口。

同一端口同时承载：WebSocket 协议、REST 健康检查、前端静态资源。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .game.models import Declaration
from .game.attributes import AttributeError_
from .net.hub import Hub
from .net.protocol import ProtocolError, parse_inbound
from .rooms import RoomError, RoomManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("yap.app")

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "web" / "dist"

hub = Hub()
manager = RoomManager(hub)
sessions: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(manager.cleanup_loop())
    logger.info("服务已启动，内置扩展包：%s", ", ".join(manager.packs))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="文字角斗场", lifespan=lifespan)


class Conn:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.player_id: Optional[str] = None
        self.room_id: Optional[str] = None
        self.token: Optional[str] = None
        self.nickname: str = "玩家"
        self.source = ws.client.host if ws.client else "unknown"


async def err(conn: Conn, code: str, message: str, fatal: bool = False) -> None:
    await conn.ws.send_json(
        {"type": "error", "code": code, "message": message, "fatal": fatal}
    )


def room_of(conn: Conn):
    return manager.get(conn.room_id) if conn.room_id else None


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "rooms": len(manager.rooms),
        "packs": [{k: v.name for k, v in manager.packs.items()}],
    }


@app.get("/api/packs")
async def packs() -> list[dict]:
    return [p.public_view() for p in manager.packs.values()]


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn = Conn(ws)
    try:
        await ws.send_json({"type": "welcome", "message": "连接成功，请先 hello"})
        while True:
            raw = await ws.receive_text()
            try:
                msg = parse_inbound(raw)
            except ProtocolError as exc:
                await err(conn, "INVALID_PAYLOAD", str(exc))
                continue
            try:
                await dispatch(conn, msg)
            except RoomError as exc:
                await err(conn, exc.code, exc.message)
            except AttributeError_ as exc:
                await err(conn, "INVALID_ATTR", str(exc))
            except ValueError as exc:
                await err(conn, "WRONG_PHASE", str(exc))
            except Exception:
                logger.exception("处理消息失败：%s", raw[:200])
                await err(conn, "INTERNAL", "服务端处理失败")
    except WebSocketDisconnect:
        pass
    finally:
        await on_disconnect(conn)


async def on_disconnect(conn: Conn) -> None:
    room = room_of(conn)
    if conn.player_id:
        unbound = await hub.unbind(conn.player_id, conn.room_id, conn.ws)
        if not unbound:
            # 同一玩家已有新连接接手（刷新/重连），旧连接收尾不能动房间席位
            logger.info("旧连接已由新连接接替，跳过房间清理：%s", conn.player_id)
            return
    if room and conn.player_id and conn.player_id in room.players:
        player = room.players[conn.player_id]
        if room.phase.value == "lobby":
            room.remove_player(conn.player_id)
            if conn.token in sessions:
                sessions[conn.token]["room_id"] = None
        else:
            player.connected = False
        await room.broadcast_state()


async def dispatch(conn: Conn, msg) -> None:
    t = msg.type

    if t == "hello":
        player_id = uuid.uuid4().hex[:12]
        token = uuid.uuid4().hex
        conn.player_id = player_id
        conn.token = token
        conn.nickname = msg.name
        sessions[token] = {"player_id": player_id, "room_id": None, "name": msg.name}
        await hub.bind(player_id, None, conn.ws)
        await conn.ws.send_json(
            {"type": "session", "token": token, "player_id": player_id, "name": msg.name}
        )
        return

    if t == "reconnect":
        data = sessions.get(msg.token)
        if not data:
            await err(conn, "AUTH_FAILED", "会话已失效，请重新加入房间", fatal=True)
            return
        conn.player_id = data["player_id"]
        conn.token = msg.token
        conn.room_id = data["room_id"]
        await hub.bind(conn.player_id, conn.room_id, conn.ws)
        room = room_of(conn)
        if room is None:
            await err(conn, "ROOM_NOT_FOUND", "房间已关闭", fatal=True)
            return
        player = room.players.get(conn.player_id)
        if player is None:
            if room.phase.value == "lobby" and not room.over:
                # 刷新页面时旧连接的关闭事件会先清掉大厅席位，凭会话原样恢复
                player = room.add_player(data.get("name") or "玩家", kind="human", pid=conn.player_id)
            else:
                await err(conn, "ROOM_NOT_FOUND", "席位已不存在", fatal=True)
                return
        player.connected = True
        await conn.ws.send_json(
            {
                "type": "session",
                "token": msg.token,
                "player_id": conn.player_id,
                "name": player.name,
            }
        )
        await conn.ws.send_json(room.snapshot_for(conn.player_id))
        await room.broadcast_state()
        return

    if conn.player_id is None:
        await err(conn, "INVALID_PAYLOAD", "请先发送 hello")
        return

    if t == "create_room":
        if conn.room_id:
            await err(conn, "INVALID_PAYLOAD", "你已在房间中")
            return
        if msg.name.strip():
            conn.nickname = msg.name.strip()
        room, host = manager.create_room(conn.nickname, msg.password, conn.player_id)
        conn.room_id = room.room_id
        sessions[conn.token].update(room_id=room.room_id, name=conn.nickname)
        await hub.move(conn.player_id, room.room_id)
        await room.broadcast_state()
        await conn.ws.send_json(room.snapshot_for(conn.player_id))
        return

    if t == "join_room":
        if conn.room_id:
            await err(conn, "INVALID_PAYLOAD", "你已在房间中")
            return
        if msg.name.strip():
            conn.nickname = msg.name.strip()
        room, player = manager.join_room(
            msg.code, msg.password, conn.nickname, conn.source, conn.player_id
        )
        conn.room_id = room.room_id
        sessions[conn.token].update(room_id=room.room_id, name=conn.nickname)
        await hub.move(conn.player_id, room.room_id)
        await room.broadcast_state()
        await conn.ws.send_json(room.snapshot_for(conn.player_id))
        return

    room = room_of(conn)
    if room is None:
        await err(conn, "ROOM_NOT_FOUND", "你还没有进入房间")
        return

    if t == "leave_room":
        if room.phase.value == "lobby":
            room.remove_player(conn.player_id)
        else:
            room.players[conn.player_id].connected = False
        await hub.unbind(conn.player_id, room.room_id)
        conn.room_id = None
        sessions[conn.token]["room_id"] = None
        await room.broadcast_state()
        return

    if t == "add_bot":
        manager.add_bot(room, conn.player_id, msg.difficulty)
        await room.broadcast_state()
        return

    if t == "remove_bot" or t == "kick":
        manager.remove_player(room, conn.player_id, msg.bot_id if t == "remove_bot" else msg.player_id)
        target = msg.bot_id if t == "remove_bot" else msg.player_id
        await hub.send_player(target, {"type": "kicked", "reason": "你被房主移出了房间"})
        await hub.unbind(target, room.room_id)
        await room.broadcast_state()
        return

    if t == "start_game":
        manager.start_game(room, conn.player_id)
        await room.broadcast_state()
        await room._broadcast({"type": "game_started", "state": room.room_state()})
        return

    if t == "set_ready":
        manager.set_ready(room, conn.player_id, msg.ready)
        await room.broadcast_state()
        return

    if t == "return_lobby":
        await manager.return_lobby(room, conn.player_id)
        await room.broadcast_state()
        await room._broadcast({"type": "lobby_returned"})
        return

    if t == "declare":
        decl = Declaration(
            actor_id=conn.player_id,
            content=msg.content,
        )
        await room.submit_declare(conn.player_id, decl)
        return

    if t == "open_attribute":
        await room.open_attribute(conn.player_id, msg.model_dump())
        return

    if t == "chat":
        await room.chat(conn.player_id, msg.text)
        return

    if t == "heartbeat":
        await conn.ws.send_json({"type": "pong"})


if DIST_DIR.exists() and (DIST_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="web")
else:

    @app.get("/", response_class=HTMLResponse)
    async def placeholder() -> str:
        return """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>文字角斗场</title>
<body style="font-family:system-ui;background:#14161c;color:#e8e6e1;padding:48px">
<h1>文字角斗场 · 后端已启动</h1>
<p>还没有找到前端构建产物 <code>web/dist</code>。</p>
<p>请在 <code>web/</code> 目录执行 <code>npm install &amp;&amp; npm run build</code>，或直接运行项目根目录的 <code>run.bat</code>。</p>
<p>健康检查：<a style="color:#e0a458" href="/api/health">/api/health</a></p>
</body></html>"""
