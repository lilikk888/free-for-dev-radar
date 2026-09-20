"""抓取 -> 解析 -> 与上一次快照对比 -> 落库。

一次 collect() 的语义：
  1. 抓最新 README（或直接用传入的文本，方便测试）
  2. 解析出全部条目
  3. 与"上一次快照"逐条目对比，产出 added / removed / updated
  4. 把本次快照和变更一起写进库

注意：对比基准是上一次快照，不是"上一次有变更的那次"。
"""

from __future__ import annotations

import hashlib
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import config
from .parser import Entry, parse

ADDED = "added"
REMOVED = "removed"
UPDATED = "updated"


class FetchError(RuntimeError):
    """抓取上游失败。"""


def fetch_source(url: str | None = None) -> str:
    request = urllib.request.Request(
        url or config.SOURCE_URL,
        headers={"User-Agent": config.USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=config.TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover - 网络路径
        raise FetchError(f"抓取 {url or config.SOURCE_URL} 失败: {exc}") from exc


def fingerprint(entry: Entry) -> str:
    """描述变化即视为条目变化，所以描述必须参与指纹。"""
    payload = "\x1f".join(
        [entry.category, entry.parent or "", entry.name, entry.url or "", entry.description]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _latest_snapshot_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def _diff(
    conn: sqlite3.Connection, previous_snapshot_id: int, entries: list[Entry]
) -> tuple[list[tuple[Any, ...]], dict[str, int]]:
    """返回 (待插入的变更行, 按类型统计的变更数)。"""
    previous = {
        row["entry_key"]: row
        for row in conn.execute(
            "SELECT * FROM entries WHERE snapshot_id = ?", (previous_snapshot_id,)
        )
    }
    current = {entry.key: entry for entry in entries}
    timestamp = _now()
    rows: list[tuple[Any, ...]] = []

    for key, entry in current.items():
        old = previous.get(key)
        if old is None:
            rows.append(
                (timestamp, ADDED, key, entry.category, entry.name, entry.url, None, entry.description)
            )
        elif old["fingerprint"] != fingerprint(entry):
            rows.append(
                (
                    timestamp,
                    UPDATED,
                    key,
                    entry.category,
                    entry.name,
                    entry.url,
                    old["description"],
                    entry.description,
                )
            )

    for key, old in previous.items():
        if key not in current:
            rows.append(
                (
                    timestamp,
                    REMOVED,
                    key,
                    old["category"],
                    old["name"],
                    old["url"],
                    old["description"],
                    None,
                )
            )

    by_type: dict[str, int] = {}
    for row in rows:
        kind = row[1]  # 行结构：(detected_at, change_type, entry_key, ...)
        by_type[kind] = by_type.get(kind, 0) + 1

    return rows, by_type


def collect(conn: sqlite3.Connection, markdown: str | None = None) -> dict[str, Any]:
    """执行一次采集。返回本次结果摘要。"""
    markdown = fetch_source() if markdown is None else markdown
    entries = parse(markdown)
    if not entries:
        raise FetchError("解析结果为空 —— 上游格式可能变了，或抓到的不是预期页面")

    previous_snapshot_id = _latest_snapshot_id(conn)
    if previous_snapshot_id is None:
        # 第一次采集没有对比基准，不算变更
        changes: list[tuple[Any, ...]] = []
        changes_by_type: dict[str, int] = {}
    else:
        changes, changes_by_type = _diff(conn, previous_snapshot_id, entries)

    timestamp = _now()
    cursor = conn.execute(
        "INSERT INTO snapshots (fetched_at, entry_count) VALUES (?, ?)",
        (timestamp, len(entries)),
    )
    snapshot_id = cursor.lastrowid
    conn.executemany(
        """INSERT INTO entries
               (snapshot_id, entry_key, category, parent, name, url, description, fingerprint)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (snapshot_id, e.key, e.category, e.parent, e.name, e.url, e.description, fingerprint(e))
            for e in entries
        ],
    )
    if changes:
        conn.executemany(
            """INSERT INTO changes
                   (detected_at, change_type, entry_key, category, name, url,
                    old_description, new_description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            changes,
        )
    conn.commit()

    return {
        "fetched_at": timestamp,
        "entry_count": len(entries),
        "changes": len(changes),
        "changes_by_type": changes_by_type,
        "baseline": previous_snapshot_id is None,
    }
