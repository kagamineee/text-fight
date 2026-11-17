"""滚动摘要：把早期账本压成一段世界史，供裁判与玩家 AI 复用。

每 10 回合触发一次，失败就沿用旧摘要，不影响对局。
"""

from __future__ import annotations

import logging
from typing import Optional

from . import judge_ai
from .models import LedgerEntry, Player

logger = logging.getLogger("yap.summary")

MAX_SUMMARY_CHARS = 300
SUMMARY_SYSTEM = """你在维护一份文字对局的世界史。把给定记录压缩成一段不超过 300 字的摘要。
只保留：谁做了什么、谁受伤或出局、出现了什么属性、场上局势。
不评论、不预测、不写建议；直接输出摘要正文，不要标题和 Markdown。"""


def _entries_text(players: dict[str, Player], entries: list[LedgerEntry]) -> str:
    lines: list[str] = []
    for entry in entries:
        if not entry.content.strip():
            continue
        actor = players[entry.actor].name if entry.actor in players else "系统"
        text = entry.content.replace("\n", " ").strip()
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"[第{entry.turn_no}回合] {actor}：{text}")
    return "\n".join(lines)


async def compress(
    *,
    previous: str,
    players: dict[str, Player],
    entries: list[LedgerEntry],
    base_url: str,
    model: str,
    timeout: float = 40.0,
) -> Optional[str]:
    record = _entries_text(players, entries)
    if not record.strip():
        return None
    user = f"""<previous_summary>
{previous or "（还没有摘要）"}
</previous_summary>

<new_records>
{record}
</new_records>

输出更新后的世界史摘要，不超过 {MAX_SUMMARY_CHARS} 字。"""
    try:
        text = await judge_ai.chat_once(
            base_url=base_url,
            model=model,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": user},
            ],
            timeout=timeout,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("世界史摘要失败：%s", exc)
        return None
    text = " ".join(text.split()).strip()
    if not text:
        return None
    return text[:MAX_SUMMARY_CHARS]
