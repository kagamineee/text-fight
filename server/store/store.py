"""埋点存储：append-only 事件日志（= 账本），SQLite 落地。

事件日志是唯一事实源（PRD §9.3）：房间状态可由事件重放重建。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  pack_id TEXT NOT NULL,
  host_player_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  closed_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  room_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  turn_no INTEGER NOT NULL,
  actor TEXT,
  type TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  intensity REAL,
  status TEXT NOT NULL DEFAULT 'active',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  PRIMARY KEY (room_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_room ON events(room_id, seq);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  player_id TEXT NOT NULL,
  room_id TEXT,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

TERMINAL_EVENTS = {"game_over"}


@dataclass
class StoredEvent:
    seq: int
    turn_no: int
    actor: Optional[str]
    type: str
    content: str
    intensity: Optional[float]
    status: str
    payload: dict
    created_at: float


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # 先探测能否创建 -journal 文件；部分 Windows 环境（安全软件 / 磁盘策略）
            # 会拒绝，导致后续写入报 "unable to open database file" / "disk I/O error"。
            try:
                self._conn.execute("CREATE TABLE IF NOT EXISTS _journal_probe (x INTEGER)")
                self._conn.execute("DROP TABLE IF EXISTS _journal_probe")
                self._conn.commit()
                self._conn.executescript(SCHEMA)
                self._conn.commit()
            except sqlite3.OperationalError:
                self._conn.rollback()
                self._conn.execute("PRAGMA journal_mode=MEMORY")
                self._conn.execute("PRAGMA synchronous=FULL")
                self._conn.executescript(SCHEMA)
                self._conn.commit()

    # ---- 房间 ----
    def create_room(
        self, room_id: str, code: str, password_hash: str, pack_id: str, host_id: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rooms (id, code, password_hash, pack_id, host_player_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (room_id, code, password_hash, pack_id, host_id, time.time()),
            )
            self._conn.commit()

    def find_room_by_code(self, code: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM rooms WHERE code = ?", (code,))
            return cur.fetchone()

    def close_room(self, room_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE rooms SET closed_at = ? WHERE id = ?", (time.time(), room_id)
            )
            self._conn.commit()

    def list_room_ids(self) -> list[str]:
        with self._lock:
            cur = self._conn.execute("SELECT id FROM rooms")
            return [row["id"] for row in cur.fetchall()]

    def room_meta(self, room_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
            return cur.fetchone()

    # ---- 会话 ----
    def save_session(self, token: str, player_id: str, room_id: Optional[str], name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (token, player_id, room_id, name, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (token, player_id, room_id, name, time.time()),
            )
            self._conn.commit()

    def load_session(self, token: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM sessions WHERE token = ?", (token,))
            return cur.fetchone()

    def set_session_room(self, token: str, room_id: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET room_id = ? WHERE token = ?", (room_id, token)
            )
            self._conn.commit()

    # ---- 事件 ----
    def append(
        self,
        room_id: str,
        turn_no: int,
        actor: Optional[str],
        type_: str,
        content: str,
        intensity: Optional[float] = None,
        payload: Optional[dict] = None,
        status: str = "active",
    ) -> StoredEvent:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE room_id = ?",
                (room_id,),
            )
            seq = int(cur.fetchone()["s"]) + 1
            now = time.time()
            self._conn.execute(
                "INSERT INTO events (room_id, seq, turn_no, actor, type, content,"
                " intensity, status, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    seq,
                    turn_no,
                    actor,
                    type_,
                    content,
                    intensity,
                    status,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()
        return StoredEvent(
            seq=seq,
            turn_no=turn_no,
            actor=actor,
            type=type_,
            content=content,
            intensity=intensity,
            status=status,
            payload=payload or {},
            created_at=now,
        )

    def events(self, room_id: str) -> list[StoredEvent]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM events WHERE room_id = ? ORDER BY seq", (room_id,)
            )
            rows = cur.fetchall()
        return [
            StoredEvent(
                seq=row["seq"],
                turn_no=row["turn_no"],
                actor=row["actor"],
                type=row["type"],
                content=row["content"],
                intensity=row["intensity"],
                status=row["status"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def has_events(self, room_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM events WHERE room_id = ? LIMIT 1", (room_id,)
            )
            return cur.fetchone() is not None
