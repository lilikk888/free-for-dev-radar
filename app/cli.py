"""命令行入口，给 K8s CronJob 用。

    python -m app.cli collect       # 抓一次上游清单并记录变更（每 6 小时）
    python -m app.cli check-links   # 巡检精选条目的官网链接（每天）
    python -m app.cli digest        # 生成并发送周报邮件（每周一）

退出码约定：0 成功 / 2 失败。**失败时也要把指标推给 Pushgateway** ——
否则「任务挂了」这件事在监控里完全看不见，等于没监控。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import collector, db, metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description="免费资源雷达")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collect", help="抓取一次上游清单并记录变更")

    lc = sub.add_parser("check-links", help="巡检精选条目的官网链接是否可达")
    lc.add_argument("--limit", type=int, default=None, help="只查前 N 条（调试用）")

    dg = sub.add_parser("digest", help="生成并发送周报邮件")
    dg.add_argument("--print-only", action="store_true", help="只打印正文不发信")

    mg = sub.add_parser("migrate", help="把 SQLite 里的数据迁移到 PostgreSQL")
    mg.add_argument("--from", dest="source", required=True, help="源 SQLite 文件路径")

    return parser


def _cmd_collect() -> int:
    started = time.monotonic()
    conn = db.connect()
    try:
        result = collector.collect(conn)
    except Exception as exc:
        metrics.record_collect(time.monotonic() - started, ok=False)
        pushed = metrics.push_job_metrics()
        print(f"采集失败: {exc}（指标已推送: {pushed}）", file=sys.stderr)
        return 2
    finally:
        conn.close()

    metrics.record_collect(
        time.monotonic() - started,
        ok=True,
        entries=result["entry_count"],
        changes=result["changes_by_type"],
    )
    result["metrics_pushed"] = metrics.push_job_metrics()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_check_links(limit: int | None) -> int:
    from . import linkcheck

    try:
        # 先同步「见过哪些条目」，周报的「本周新收录」靠它算
        linkcheck.sync_seen()
        result = linkcheck.check_all(limit=limit)
    except Exception as exc:
        metrics.record_linkcheck(0, 0)
        pushed = metrics.push_job_metrics()
        print(f"巡检失败: {exc}（指标已推送: {pushed}）", file=sys.stderr)
        return 2

    metrics.record_linkcheck(result["total"], result["dead"])
    result["metrics_pushed"] = metrics.push_job_metrics()
    # ⚠️ 不因为个别站点不可达就返回失败 —— 那很正常，交给指标和告警去关注
    print(json.dumps({k: v for k, v in result.items() if k != "dead_entries"}, ensure_ascii=False))
    if result["dead"]:
        print("不可达的链接：", file=sys.stderr)
        for d in result["dead_entries"]:
            print(f"  - {d['name']}  {d['url']}  {d['error']}", file=sys.stderr)
    return 0


def _cmd_digest(print_only: bool) -> int:
    from . import digest

    subject, html, stats = digest.build()
    print(json.dumps({"subject": subject, **stats}, ensure_ascii=False))
    if print_only:
        print(html)
        return 0

    # 没有任何内容就不发，避免变成每周噪音（最后被自己拉黑）
    if not (stats["fresh"] or stats["due"] or stats["dead"]):
        print("本周没有值得汇报的内容，跳过发信")
        return 0

    ok, msg = digest.send(subject, html)
    print(("已发送: " if ok else "未发送: ") + msg)
    return 0 if (ok or "未配置 SMTP" in msg) else 2


def _cmd_migrate(source: str) -> int:
    from . import database, migrate

    if not database.using_postgres():
        print("目标库不是 PostgreSQL（没配 RADAR_DB_URL），迁移没有意义", file=sys.stderr)
        return 2
    try:
        report = migrate.migrate(source)
    except Exception as exc:
        print(f"迁移失败: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "collect":
        return _cmd_collect()
    if args.command == "check-links":
        return _cmd_check_links(args.limit)
    if args.command == "digest":
        return _cmd_digest(args.print_only)
    if args.command == "migrate":
        return _cmd_migrate(args.source)
    return 1  # pragma: no cover - argparse 已经拦截


if __name__ == "__main__":
    sys.exit(main())
