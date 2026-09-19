"""命令行入口，给 K8s CronJob 用。

    python -m app.cli collect      # 抓一次并记录变更
"""

from __future__ import annotations

import argparse
import json
import sys

from . import collector, db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description="free-for-dev 变更雷达")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="抓取一次上游并记录变更")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "collect":
        conn = db.connect()
        try:
            result = collector.collect(conn)
        finally:
            conn.close()
        print(json.dumps(result, ensure_ascii=False))
        return 0

    return 1  # pragma: no cover - argparse 已经拦截


if __name__ == "__main__":
    sys.exit(main())
