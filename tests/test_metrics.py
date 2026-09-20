"""指标相关的测试。

两个关键不变量：
1. Web 的 /metrics 必须反映**库里真实的状态**（不是进程内缓存的陈旧数字）
2. 采集失败也必须能产生可告警的指标 —— 否则任务挂了监控里什么都看不到
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from app import collector, db, metrics
from app.web import app

FIXTURE = (Path(__file__).parent / "fixtures" / "sample.md").read_text(encoding="utf-8")
MAILER_LINE = "  * [Mailer](https://mailer.example) - 100 emails per day\n"


def _added(text: str) -> str:
    return text.replace(
        MAILER_LINE, MAILER_LINE + "  * [New Mailer](https://new.example) - 1000 per month\n"
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """把指标模块用到的 db.connect 指向临时库，避免污染真实数据。

    注意：metrics.db 就是 db 模块本身，所以必须先抓住原函数再 patch，
    否则 lambda 里再调 db.connect 会递归到爆栈。
    """
    real_connect = db.connect
    monkeypatch.setattr(
        metrics.db, "connect", lambda *a, **k: real_connect(tmp_path / "radar.db")
    )
    seed = real_connect(tmp_path / "radar.db")
    try:
        collector.collect(seed, FIXTURE)
        collector.collect(seed, _added(FIXTURE))
    finally:
        seed.close()
    return TestClient(app)


def test_collect_reports_changes_by_type():
    """collect() 要返回按类型分组的变更数，供 CronJob 推指标用。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "radar.db")
        try:
            first = collector.collect(conn, FIXTURE)
            assert first["baseline"] is True
            assert first["changes_by_type"] == {}

            second = collector.collect(conn, _added(FIXTURE))
            assert second["changes"] == 1
            assert second["changes_by_type"] == {"added": 1}
        finally:
            conn.close()


def test_metrics_endpoint_exposes_db_state(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text

    # 第一次 7 条 + 第二次加了 1 条 = 8 条
    assert "radar_entries_total 8.0" in body
    assert "radar_snapshots_total 2.0" in body
    assert 'radar_changes_total{change_type="added"} 1.0' in body
    # 新鲜度指标必须存在，告警规则依赖它
    assert "radar_last_snapshot_age_seconds" in body


def test_metrics_reflects_live_db_not_cache(client, tmp_path, monkeypatch):
    """再采一次后，指标必须跟着变 —— 证明是每次抓取现查库。"""
    before = client.get("/metrics").text
    assert "radar_snapshots_total 2.0" in before

    conn = db.connect(tmp_path / "radar.db")
    try:
        collector.collect(conn, _added(FIXTURE))
    finally:
        conn.close()

    after = client.get("/metrics").text
    assert "radar_snapshots_total 3.0" in after


def test_http_requests_are_counted(client):
    client.get("/healthz")
    body = client.get("/metrics").text
    assert 'radar_http_requests_total{method="GET",path="/healthz",status="200"}' in body


def test_unknown_paths_collapse_to_other(client):
    """未匹配的路由不能把原始路径写进标签，否则标签基数会被扫描器打爆。"""
    client.get("/some/random/scanner/path")
    body = client.get("/metrics").text
    assert 'path="/some/random/scanner/path"' not in body
    assert 'path="other"' in body


def test_job_metrics_record_success():
    metrics.record_collect(
        1.25, ok=True, entries=1334, changes={"added": 2, "removed": 1}
    )
    body = generate_latest(metrics.JOB_REGISTRY).decode()
    assert "radar_collect_success 1.0" in body
    assert "radar_collect_entries 1334.0" in body
    assert "radar_collect_duration_seconds 1.25" in body
    assert 'radar_collect_changes{change_type="added"} 2.0' in body
    assert 'radar_collect_changes{change_type="removed"} 1.0' in body


def test_job_metrics_record_failure_is_alertable():
    """采集失败必须让 radar_collect_success 变成 0，否则没法配告警。"""
    metrics.record_collect(0.4, ok=False)
    body = generate_latest(metrics.JOB_REGISTRY).decode()
    assert "radar_collect_success 0.0" in body
    # 上一次成功的 changes 标签要被清掉，不能留着误导人
    assert 'radar_collect_changes{change_type="added"}' not in body


def test_push_is_skipped_without_gateway(monkeypatch):
    """没配 Pushgateway 地址时要静默跳过，不能抛异常（本地开发 / 单测依赖它）。"""
    monkeypatch.setattr(metrics, "PUSHGATEWAY_URL", "")
    assert metrics.push_job_metrics() is False
