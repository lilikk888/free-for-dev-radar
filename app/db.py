"""存储层入口（现在只是 `database` 的转发层，保持调用方 API 不变）。

历史：这里原本只支持 SQLite（单文件 + WAL，挂 PVC，Web 和 CronJob 共用）。
要支持多副本 + HPA 时，RWO 卷就成了硬限制 —— 两个副本跨节点没法挂同一个文件。

所以表结构和连接逻辑搬到了 `app/database.py`，同时支持：
    - 没配 RADAR_DB_URL → SQLite（本地开发、单测、单节点部署）
    - 配了 RADAR_DB_URL → PostgreSQL（多副本、真正的并发写）

**业务代码一行都没改** —— 大家还是 `db.connect()`，所以这个迁移是可控的。
"""

from __future__ import annotations

from pathlib import Path

from . import config
from . import database
from .database import Connection, using_postgres

__all__ = ["connect", "Connection", "using_postgres"]


def connect(db_path: Path | str | None = None) -> Connection:
    """打开连接并确保表结构就绪。"""
    if using_postgres():
        return database.connect_postgres(database.DB_URL)
    return database.connect_sqlite(db_path or config.DB_PATH)
