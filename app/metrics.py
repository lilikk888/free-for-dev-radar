"""Prometheus 指标定义。

这里分两类，因为「常驻进程」和「短命进程」的暴露方式根本不同：

1. **Web 进程（常驻）** → 自己提供 `/metrics` 端点给 Prometheus 抓
   - HTTP 请求计数
   - 库里状态的实时快照（用自定义 Collector，每次被抓取时才现查 SQLite）

2. **CronJob 进程（跑几秒就退出）** → Prometheus **根本抓不到它**
   （抓的时候进程早没了）。这正是 Pushgateway 存在的意义：
   短命任务把结果「推」到 Pushgateway，Prometheus 再去抓 Pushgateway。

所以「业务指标」拆成两个来源，用不同的 registry 隔离：
- WEB_REGISTRY：暴露在 Web Pod 的 /metrics 上
- JOB_REGISTRY：由 CronJob 推送到 Pushgateway

> 为什么不用 `prometheus_client.REGISTRY`（默认全局注册表）：
> 同一个进程里两类指标混在一起，推送时会把 Web 的指标也一起推上去。
> 显式建两个 registry 更干净。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Iterator

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

from . import db

PUSHGATEWAY_URL = os.getenv("RADAR_PUSHGATEWAY_URL", "")
PUSH_JOB_NAME = os.getenv("RADAR_PUSH_JOB_NAME", "radar-collect")

# ---------------------------------------------------------------- Web 侧

WEB_REGISTRY = CollectorRegistry()

# path 用路由模板而不是原始路径，避免 404 扫描把标签基数打爆
HTTP_REQUESTS = Counter(
    "radar_http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
    registry=WEB_REGISTRY,
)


def _parse_iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


class RadarStateCollector:
    """把「库里的业务状态」暴露成指标。

    刻意做成 Collector 而不是定时更新的 Gauge：
    每次 Prometheus 抓取时才查库，所以指标**永远是最新的**，
    也不用在 Web 进程里跑后台任务。
    """

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            conn = db.connect()
        except Exception:  # pragma: no cover - 库还没建好时不要让抓取直接 500
            return
        try:
            snapshots = conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"]
            yield GaugeMetricFamily(
                "radar_snapshots_total", "历史快照总数（每次采集产生一个）", value=snapshots
            )

            latest = conn.execute(
                "SELECT fetched_at, entry_count FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()

            # ⚠️ 注意：GaugeMetricFamily 不要传 value=...，那会额外塞一个默认样本，
            # 再 add_metric 就变成「同名同标签两个样本」，Prometheus 会以
            # duplicate sample 为由**拒绝整个抓取**。一律只用 add_metric。
            entries = GaugeMetricFamily("radar_entries_total", "最近一次快照解析出的条目总数")
            ts_family = GaugeMetricFamily(
                "radar_last_snapshot_timestamp_seconds", "最近一次采集完成时的 Unix 时间戳"
            )
            age = GaugeMetricFamily(
                "radar_last_snapshot_age_seconds",
                "距最近一次成功采集已经过去多少秒（采集挂了它会一直涨，用来报警）",
            )

            if latest is not None:
                entries.add_metric([], latest["entry_count"])
                parsed = _parse_iso(latest["fetched_at"])
                if parsed is not None:
                    ts_family.add_metric([], parsed)
                    age.add_metric([], max(0.0, time.time() - parsed))
            else:
                # 从没成功采集过。这两个指标刻意不输出（而不是输出 NaN 或 0）——
                # 「没有数据」和「数据是 0」是两件事，用 absent() 报警更准确。
                entries.add_metric([], 0)

            yield entries
            yield ts_family
            yield age

            changes = GaugeMetricFamily(
                "radar_changes_total", "累计发现的变更数", labels=["change_type"]
            )
            for row in conn.execute(
                "SELECT change_type, COUNT(*) AS c FROM changes GROUP BY change_type"
            ):
                changes.add_metric([row["change_type"]], row["c"])
            yield changes
        finally:
            conn.close()


WEB_REGISTRY.register(RadarStateCollector())


class CreditsCollector:
    """把「我的免费额度到期情况」暴露成指标。

    ⭐ 这里体现一个设计取舍：到期提醒**不自己写发信代码**，
    而是把业务状态转成指标，交给已经跑通的那条链路处理：

        我的领用记录（SQLite）→ /metrics 指标 → Prometheus 规则 → Alertmanager → 163 邮件

    好处：零新增 SMTP 凭据、零新增发信代码、且顺带能看到「快到期额度」的历史曲线。
    这也是把「业务事件」接入「统一告警体系」的标准做法。
    """

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            from . import userdata

            rows = userdata.list_all()
        except Exception:  # pragma: no cover - 表还没建好时不要影响抓取
            return

        tracked = GaugeMetricFamily(
            "radar_credits_tracked_total", "我记录的免费额度条数（不含已放弃）"
        )
        tracked.add_metric([], sum(1 for r in rows if r["status"] != "dropped"))
        yield tracked

        registered = GaugeMetricFamily(
            "radar_credits_registered_total", "已注册的免费额度条数"
        )
        registered.add_metric([], sum(1 for r in rows if r["status"] == "registered"))
        yield registered

        # 距离到期还有多少秒（负数代表已经过期）。标签基数受限于用户自己的记录数，
        # 不会失控（这是"我自己的清单"，不是全站数据）。
        secs = GaugeMetricFamily(
            "radar_credits_expiring_seconds",
            "距额度到期还有多少秒（负数=已过期）",
            labels=["entry_id", "name"],
        )
        soon = 0
        for r in rows:
            if r["status"] != "registered" or r["expires_at"] is None:
                continue
            d = r["days_left"]
            if d is None:
                continue
            from datetime import date

            try:
                exp = date.fromisoformat(r["expires_at"])
            except ValueError:
                continue
            delta = (exp - date.today()).total_seconds()
            secs.add_metric([r["entry_id"], r["name"] or r["entry_id"]], delta)
            if d <= 14:
                soon += 1
        yield secs

        # 告警就看这一个：>0 说明有额度快到期（含已过期）
        count = GaugeMetricFamily(
            "radar_credits_due_soon",
            "14 天内到期或已过期的额度数量（用来触发提醒告警）",
        )
        count.add_metric([], soon)
        yield count


WEB_REGISTRY.register(CreditsCollector())


def render_web_metrics() -> tuple[bytes, str]:
    return generate_latest(WEB_REGISTRY), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------- CronJob 侧

JOB_REGISTRY = CollectorRegistry()

COLLECT_DURATION = Gauge(
    "radar_collect_duration_seconds",
    "最近一次采集耗时（秒）",
    registry=JOB_REGISTRY,
)
COLLECT_ENTRIES = Gauge(
    "radar_collect_entries", "最近一次采集解析出的条目数", registry=JOB_REGISTRY
)
COLLECT_CHANGES = Gauge(
    "radar_collect_changes",
    "最近一次采集发现的变更数",
    ["change_type"],
    registry=JOB_REGISTRY,
)
COLLECT_SUCCESS = Gauge(
    "radar_collect_success",
    "最近一次采集是否成功（1 成功 / 0 失败）",
    registry=JOB_REGISTRY,
)


def record_collect(
    duration: float,
    *,
    ok: bool,
    entries: int = 0,
    changes: dict[str, int] | None = None,
    finished_at: float | None = None,
) -> None:
    """记录一次采集的结果。失败时只有 ok 和相关指标被置位。"""
    COLLECT_DURATION.set(duration)
    COLLECT_ENTRIES.set(entries)
    COLLECT_SUCCESS.set(1 if ok else 0)
    COLLECT_CHANGES.clear()
    for change_type, count in (changes or {}).items():
        COLLECT_CHANGES.labels(change_type).set(count)
    # prometheus_client 会自动带 process_start_time_seconds，这里不用手动记时间戳


def push_job_metrics() -> bool:
    """把本次采集结果推给 Pushgateway。

    没配 RADAR_PUSHGATEWAY_URL 就静默跳过 —— 本地开发 / 单测里不该依赖它。
    推送失败不抛异常：采集本身已经成功，不该因为监控挂了而让任务失败。
    """
    if not PUSHGATEWAY_URL:
        return False
    try:
        from prometheus_client import push_to_gateway

        push_to_gateway(PUSHGATEWAY_URL, job=PUSH_JOB_NAME, registry=JOB_REGISTRY)
        return True
    except Exception:  # pragma: no cover - 网络路径
        return False
