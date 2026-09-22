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

-- 「我的领用记录」：用户自己标的状态。
-- entry_id 指向 data_curated.py 里的精选 id（例如 zhipu / aliyun-bailian）。
-- 免费额度大多有期限，所以 expires_at 是这张表的重点字段 ——
-- 到期前要能提醒用户，否则领了 2000 万 tokens 忘了用，白过期。
CREATE TABLE IF NOT EXISTS mine (
    entry_id      TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'interested',  -- interested / registered / dropped
    registered_at TEXT,
    expires_at    TEXT,      -- ISO 日期，用户填的额度到期日
    note          TEXT,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mine_expires_at ON mine(expires_at);
CREATE INDEX IF NOT EXISTS idx_mine_status ON mine(status);

-- 链接巡检结果。
-- 为什么需要：免费服务会关停、官网会改版，清单里的链接会悄悄失效。
-- 与其等用户点进去发现 404，不如定时自己巡一遍。
-- 每次巡检**追加**一行（不是覆盖），这样能看出「某条链接是从哪天开始挂的」。
CREATE TABLE IF NOT EXISTS link_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT    NOT NULL,
    checked_at  TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    status_code INTEGER,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_link_checks_entry ON link_checks(entry_id, id DESC);

-- 「见过哪些精选条目」，用于每周订阅邮件算出「本周新收录」。
-- 每次跑巡检/采集时把当前精选 id 全量 upsert 进去，first_seen 只写一次。
CREATE TABLE IF NOT EXISTS seen_entries (
    entry_id   TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
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
