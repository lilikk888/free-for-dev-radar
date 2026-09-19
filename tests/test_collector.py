"""采集与变更检测测试。

核心不变量：内容没变就不能产生变更记录，否则这个雷达会天天误报。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import collector, db
from app.collector import ADDED, REMOVED, UPDATED

FIXTURE = (Path(__file__).parent / "fixtures" / "sample.md").read_text(encoding="utf-8")
MAILER_LINE = "  * [Mailer](https://mailer.example) - 100 emails per day\n"


def _updated(text: str) -> str:
    return text.replace("5GB free, 1GB egress", "5GB free, 2GB egress")


def _removed(text: str) -> str:
    return text.replace(MAILER_LINE, "")


def _added(text: str) -> str:
    return text.replace(MAILER_LINE, MAILER_LINE + "  * [New Mailer](https://new.example) - 1000 per month\n")


def _changes(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM changes ORDER BY id")]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "radar.db")
    yield connection
    connection.close()


def test_first_run_is_baseline(conn):
    result = collector.collect(conn, FIXTURE)
    assert result["baseline"] is True
    assert result["changes"] == 0
    assert result["entry_count"] == 7
    assert _changes(conn) == []


def test_identical_rerun_reports_nothing(conn):
    collector.collect(conn, FIXTURE)
    result = collector.collect(conn, FIXTURE)
    assert result["baseline"] is False
    assert result["changes"] == 0
    assert _changes(conn) == []


def test_detects_description_update(conn):
    collector.collect(conn, FIXTURE)
    collector.collect(conn, _updated(FIXTURE))

    changes = _changes(conn)
    assert len(changes) == 1
    change = changes[0]
    assert change["change_type"] == UPDATED
    assert change["name"] == "Object Storage"
    assert change["old_description"] == "5GB free, 1GB egress"
    assert change["new_description"] == "5GB free, 2GB egress"


def test_detects_removed_entry(conn):
    collector.collect(conn, FIXTURE)
    result = collector.collect(conn, _removed(FIXTURE))

    assert result["changes"] == 1
    assert result["entry_count"] == 6
    change = _changes(conn)[0]
    assert change["change_type"] == REMOVED
    assert change["name"] == "Mailer"
    assert change["old_description"] == "100 emails per day"
    assert change["new_description"] is None


def test_detects_added_entry(conn):
    collector.collect(conn, FIXTURE)
    result = collector.collect(conn, _added(FIXTURE))

    assert result["changes"] == 1
    assert result["entry_count"] == 8
    change = _changes(conn)[0]
    assert change["change_type"] == ADDED
    assert change["name"] == "New Mailer"
    assert change["new_description"] == "1000 per month"


def test_snapshot_history_accumulates(conn):
    collector.collect(conn, FIXTURE)
    collector.collect(conn, _updated(FIXTURE))

    snapshots = [dict(r) for r in conn.execute("SELECT * FROM snapshots ORDER BY id")]
    assert len(snapshots) == 2
    assert snapshots[0]["entry_count"] == 7
    assert snapshots[1]["entry_count"] == 7

    entries = conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]
    assert entries == 14


def test_empty_parse_result_is_rejected(conn):
    """上游格式变了要显式报错，不能静默清空历史。"""
    collector.collect(conn, FIXTURE)
    with pytest.raises(collector.FetchError):
        collector.collect(conn, "# 完全不是预期格式的内容\n\n没有列表项\n")
