"""文字角斗场 · 服务端入口。

同一端口同时承载：WebSocket 协议、REST 健康检查、前端静态资源。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .game import judge_ai
from .game.models import Declaration
from .net.hub import Hub
from .net.protocol import ProtocolError, parse_inbound
from .packs.loader import (
    MAX_PACK_BYTES,
    PACK_FIELD_GUIDE,
    PACK_TEMPLATE,
    user_packs_dir,
    validate_pack_text,
)
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
LOBBY_RECONNECT_SECONDS = 120
SESSION_TTL = 24 * 3600


async def cleanup_once(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for token, data in list(sessions.items()):
        pid = data["player_id"]
        if hub.is_connected(pid):
            continue
        age = now - data.get("last_seen", now)
        room = manager.get(data.get("room_id"))
        if room and room.phase.value == "lobby" and age >= LOBBY_RECONNECT_SECONDS:
            room.remove_player(pid)
            data["room_id"] = None
            await room.broadcast_state()
        if age >= SESSION_TTL and (room is None or room.phase.value in ("lobby", "over")):
            sessions.pop(token, None)
    await manager.cleanup_once(now)
    for token, data in list(sessions.items()):
        if data.get("room_id") and manager.get(data["room_id"]) is None:
            if hub.is_connected(data["player_id"]):
                data["room_id"] = None
                await hub.move(data["player_id"], None)
                await hub.send_player(data["player_id"], {"type": "kicked", "reason": "房间已到期关闭"})
            else:
                sessions.pop(token, None)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(30)
        await cleanup_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup_loop())
    logger.info("服务已启动，可用扩展包：%s", ", ".join(manager.packs))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for room in list(manager.rooms.values()):
            await room.stop()


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
        "packs": {k: v.name for k, v in manager.packs.items()},
    }


@app.get("/api/packs")
async def packs() -> dict:
    manager.refresh_packs()
    return {
        "packs": [p.public_view() for p in manager.packs.values()],
        "issues": manager.registry.issues,
        "default": manager.default_pack_id,
    }


@app.post("/api/packs/reload")
async def reload_packs() -> dict:
    return manager.reload_packs()


DEV_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _dev_allowed(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in DEV_HOSTS


def _dev_denied() -> dict:
    return {"ok": False, "errors": ["开发者接口只允许本机访问。"]}


def _pack_summary(pack) -> dict:
    return {
        "id": pack.id,
        "name": pack.name,
        "events": len(pack.events),
        "hp": pack.hp.base,
    }


def _save_pack(text, pack) -> dict:
    """导入与开发面板共用同一保存路径，并检查实际加载结果。"""
    manager.refresh_packs()
    target_dir = user_packs_dir()
    tmp_path = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = manager.registry.user_files.get(pack.id, target_dir / f"{pack.id}.yaml")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=target_dir, suffix=".tmp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(text)
        os.replace(tmp_path, path)
        reload_result = manager.reload_packs()
    except OSError:
        return {"ok": False, "errors": ["扩展包写入失败，请检查用户包目录的权限。"]}
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    active = manager.packs.get(pack.id)
    if active is None or active.model_dump() != pack.model_dump():
        return {"ok": False, "errors": ["文件已保存，但加载结果不一致，请检查重名包或加载错误。"],
                "reload": reload_result}
    return {"ok": True, "file": str(path), "errors": [],
            "pack": _pack_summary(active), "reload": reload_result}


@app.get("/api/dev/schema")
async def dev_schema(request: Request) -> dict:
    if not _dev_allowed(request):
        return _dev_denied()
    return {"ok": True, "example": PACK_TEMPLATE, "fields": PACK_FIELD_GUIDE}


@app.post("/api/packs/import")
async def import_pack(request: Request) -> dict:
    """导入本机选择的 YAML；先完整校验，同 id 必须明确确认覆盖。"""
    if not _dev_allowed(request):
        return _dev_denied()
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_PACK_BYTES * 6 + 4096:
            return {"ok": False, "errors": [f"扩展包文件不能超过 {MAX_PACK_BYTES // 1024} KB。"]}
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "errors": ["导入请求格式错误。"]}
    if not isinstance(body, dict) or not isinstance(body.get("yaml"), str):
        return {"ok": False, "errors": ["请选择 UTF-8 编码的 YAML 扩展包。"]}
    text = body["yaml"].lstrip("\ufeff")
    pack, errors = validate_pack_text(text)
    if pack is None:
        return {"ok": False, "errors": errors}
    manager.refresh_packs()
    if pack.id in manager.packs and body.get("overwrite") is not True:
        return {"ok": False, "conflict": True, "pack": _pack_summary(pack),
                "errors": [f"已存在 id 为 {pack.id} 的扩展包，是否替换？"]}
    return _save_pack(text, pack)


@app.post("/api/dev/validate")
async def dev_validate(request: Request) -> dict:
    if not _dev_allowed(request):
        return _dev_denied()
    body = await request.json()
    pack, errors = validate_pack_text(str(body.get("yaml", "")))
    return {
        "ok": not errors,
        "errors": errors,
        "pack": _pack_summary(pack) if pack else None,
    }


@app.post("/api/dev/draft")
async def dev_draft(request: Request) -> dict:
    if not _dev_allowed(request):
        return _dev_denied()
    body = await request.json()
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return {"ok": False, "errors": ["先写一句世界观设想。"]}
    base_url = body.get("base_url") or None
    try:
        text = await judge_ai.draft_pack(prompt, base_url=base_url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [f"AI 生成失败：{exc}"]}
    pack, errors = validate_pack_text(text)
    return {
        "ok": not errors,
        "yaml": text,
        "errors": errors,
        "pack": _pack_summary(pack) if pack else None,
    }


@app.post("/api/dev/save")
async def dev_save(request: Request) -> dict:
    if not _dev_allowed(request):
        return _dev_denied()
    body = await request.json()
    text = str(body.get("yaml", ""))
    pack, errors = validate_pack_text(text)
    if pack is None:
        return {"ok": False, "errors": errors}
    return _save_pack(text, pack)


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
        player.connected = False
        if room.phase.value == "lobby":
            player.ready = False
        room.reassign_host()
        await room.broadcast_state()
    if conn.token in sessions:
        sessions[conn.token]["last_seen"] = time.time()


async def dispatch(conn: Conn, msg) -> None:
    t = msg.type

    if t == "hello":
        if conn.player_id is not None:
            await err(conn, "INVALID_PAYLOAD", "当前连接已建立会话")
            return
        player_id = uuid.uuid4().hex[:12]
        token = uuid.uuid4().hex
        conn.player_id = player_id
        conn.token = token
        conn.nickname = msg.name
        sessions[token] = {"player_id": player_id, "room_id": None, "name": msg.name,
                           "last_seen": time.time()}
        await hub.bind(player_id, None, conn.ws)
        await conn.ws.send_json(
            {"type": "session", "token": token, "player_id": player_id, "name": msg.name,
             "room_id": None}
        )
        return

    if t == "reconnect":
        if conn.player_id is not None:
            await err(conn, "INVALID_PAYLOAD", "当前连接已建立会话")
            return
        data = sessions.get(msg.token)
        if not data:
            await err(conn, "AUTH_FAILED", "会话已失效，请重新加入房间", fatal=True)
            return
        room = manager.get(data.get("room_id"))
        if (room and room.phase.value == "lobby" and not hub.is_connected(data["player_id"])
                and time.time() - data.get("last_seen", time.time()) >= LOBBY_RECONNECT_SECONDS):
            room.remove_player(data["player_id"])
            data["room_id"] = None
            await room.broadcast_state()
            room = None
        if data.get("room_id") and (room is None or data["player_id"] not in room.players):
            data["room_id"] = None
            await err(conn, "ROOM_NOT_FOUND", "房间或席位已不存在", fatal=True)
            return
        if (not hub.is_connected(data["player_id"])
                and time.time() - data.get("last_seen", time.time()) >= SESSION_TTL
                and (room is None or room.phase.value in ("lobby", "over"))):
            sessions.pop(msg.token, None)
            await err(conn, "AUTH_FAILED", "会话已过期，请重新加入房间", fatal=True)
            return
        conn.player_id = data["player_id"]
        conn.token = msg.token
        conn.room_id = data["room_id"]
        conn.nickname = data.get("name") or "玩家"
        data["last_seen"] = time.time()
        await hub.bind(conn.player_id, conn.room_id, conn.ws)
        if room:
            player = room.players[conn.player_id]
            player.connected = True
            room.reassign_host()
        await conn.ws.send_json(
            {
                "type": "session",
                "token": msg.token,
                "player_id": conn.player_id,
                "name": conn.nickname,
                "room_id": conn.room_id,
            }
        )
        if room:
            await conn.ws.send_json(room.snapshot_for(conn.player_id))
            await room.broadcast_state()
        return

    if conn.player_id is None:
        await err(conn, "INVALID_PAYLOAD", "请先发送 hello")
        return

    if conn.token not in sessions or not hub.is_current(conn.player_id, conn.ws):
        await err(conn, "AUTH_FAILED", "连接已被替换，请重新连接", fatal=True)
        return
    data = sessions[conn.token]
    data["last_seen"] = time.time()
    conn.room_id = data.get("room_id")

    if t == "create_room":
        if conn.room_id:
            await err(conn, "INVALID_PAYLOAD", "你已在房间中")
            return
        if msg.name.strip():
            conn.nickname = msg.name.strip()
        room, host = manager.create_room(
            conn.nickname, msg.password, conn.player_id, msg.pack_id
        )
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
            room.reassign_host()
        await hub.move(conn.player_id, None)
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
        for data in sessions.values():
            if data["player_id"] == target:
                data["room_id"] = None
        await hub.move(target, None)
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
