"""[TEMP-LOG] 临时调试日志模块 —— 采集裁判与结算异常，供赛后分析。

⚠️ 这是临时模块：测试稳定后整体删除。
删除清单：
  1. 删除本文件；
  2. 全局搜索 "TEMP-LOG" 清除所有调用点；
  3. 删除 logs/judge_debug.jsonl。

日志写入项目根 logs/judge_debug.jsonl（JSON Lines）。
环境变量 YAP_DEBUG_LOG=0 可临时关闭，不影响主流程。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "judge_debug.jsonl"
_enabled: bool | None = None


def _ok() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("YAP_DEBUG_LOG", "1") != "0"
    return _enabled


def log(event: str, **fields: Any) -> None:
    """追加一条调试日志。任何异常都吞掉，绝不影响游戏主流程。"""
    if not _ok():
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {"t": round(time.time(), 3), "event": event, **fields}
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
