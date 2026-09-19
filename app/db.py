"""SQLite 存储层。

设计取舍：单文件 SQLite + WAL，不需要额外数据库服务。
K8s 里挂在 PVC 上，CronJob 采集进程和 Web 进程共用同一个文件。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  TEXT    NOT NULL,
    entry_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    entry_key   TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    parent      TEXT,
    name        TEXT    NOT NULL,
    url         TEXT,
    description TEXT    NOT NULL DEFAULT '',
    fingerprint TEXT    NOT NULL,
    PRIMARY KEY (snapshot_id, entry_key)
);

CREATE TABLE IF NOT EXISTS changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT    NOT NULL,
    change_type     TEXT    NOT NULL,
    entry_key       TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    url             TEXT,
    old_description TEXT,
    new_description TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_detected_at ON changes(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_entry_key ON changes(entry_key);
CREATE INDEX IF NOT EXISTS idx_changes_type ON changes(change_type);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开连接并确保表结构就绪。"""
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
