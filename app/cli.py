"""命令行入口，给 K8s CronJob 用。

    python -m app.cli collect      # 抓一次并记录变更

退出码：0 成功 / 2 采集失败。采集失败时**也要**把指标推给 Pushgateway ——
否则「任务挂了」这件事在监控里完全看不见，等于没监控。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import collector, db, metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description="free-for-dev 变更雷达")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="抓取一次上游并记录变更")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "collect":
        started = time.monotonic()
        conn = db.connect()
        try:
            result = collector.collect(conn)
        except Exception as exc:
            metrics.record_collect(time.monotonic() - started, ok=False)
            pushed = metrics.push_job_metrics()
            print(
                f"采集失败: {exc}（指标已推送: {pushed}）",
                file=sys.stderr,
            )
            return 2
        finally:
            conn.close()

        metrics.record_collect(
            time.monotonic() - started,
            ok=True,
            entries=result["entry_count"],
            changes=result["changes_by_type"],
        )
        pushed = metrics.push_job_metrics()
        result["metrics_pushed"] = pushed
        print(json.dumps(result, ensure_ascii=False))
        return 0

    return 1  # pragma: no cover - argparse 已经拦截


if __name__ == "__main__":
    sys.exit(main())
