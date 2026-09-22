"""把数据从 SQLite 搬到 PostgreSQL（一次性迁移工具）。

为什么要有它：切数据库最大的风险是**历史数据丢了** ——
1335 条资源快照、变更记录、我的领用记录都在那个 SQLite 文件里。
没有迁移工具就只能重新采集，变更历史永久丢失。

用法（在集群里跑一次性 Job）：
    python -m app.cli migrate --from /data/radar.db

判重策略：迁移可重复执行（幂等）。
- snapshots / entries / changes / link_checks：按主键判重，已存在就跳过
- mine / seen_entries：按主键 upsert（用现有值覆盖）

⚠️ 不迁移 id 自增序列：迁完之后目标库里新插入的行会从 max(id)+1 开始
（PG 的 IDENTITY 会自动处理），所以不需要手工修序列。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import db

# (表名, 主键列, 是否用 upsert)
TABLES: list[tuple[str, list[str], bool]] = [
    ("snapshots", ["id"], False),
    ("entries", ["snapshot_id", "entry_key"], False),
    ("changes", ["id"], False),
    ("mine", ["entry_id"], True),
    ("link_checks", ["id"], False),
    ("seen_entries", ["entry_id"], True),
]


def _cols(conn_sqlite: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn_sqlite.execute(f"PRAGMA table_info({table})")]


def migrate(source: str | Path) -> dict[str, Any]:
    src_path = Path(source)
    if not src_path.exists():
        raise FileNotFoundError(f"源库不存在: {src_path}")

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    dst = db.connect()

    report: dict[str, Any] = {"source": str(src_path), "tables": {}}
    try:
        for table, pk, upsert in TABLES:
            try:
                cols = _cols(src, table)
            except sqlite3.DatabaseError:
                report["tables"][table] = {"skipped": "源库里没有这张表"}
                continue
            rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
            if not rows:
                report["tables"][table] = {"rows": 0, "inserted": 0}
                continue

            placeholders = ", ".join("?" for _ in cols)
            collist = ", ".join(cols)
            if upsert:
                updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in pk)
                sql = (
                    f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"
                )
            else:
                # 幂等：主键冲突就跳过（PG 支持 DO NOTHING，SQLite 3.24+ 也支持）
                sql = (
                    f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({', '.join(pk)}) DO NOTHING"
                )

            inserted = 0
            for r in rows:
                cur = dst.execute(sql, [r.get(c) for c in cols])
                if cur.rowcount and cur.rowcount > 0 and not upsert:
                    inserted += 1
            dst.commit()
            report["tables"][table] = {"rows": len(rows), "inserted": inserted if not upsert else len(rows)}

        # 迁移后做个基本校验：条目数对不对得上
        latest = dst.execute("SELECT MAX(id) AS id FROM snapshots").fetchone()
        if latest and latest["id"] is not None:
            cnt = dst.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE snapshot_id = ?", (latest["id"],)
            ).fetchone()
            report["verify"] = {"latest_snapshot_id": latest["id"], "entries_of_latest": cnt["c"]}
    finally:
        src.close()
        dst.close()

    return report
