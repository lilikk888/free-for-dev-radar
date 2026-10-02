"""迁移工具的测试。

重点是锁住一条真实事故教训：**迁移 PostgreSQL 后必须重置自增序列**。

真实事故（2026-10-02 才发现）：
  迁移时为了保留原 id 会显式插入 id，但 PG 的 IDENTITY 序列不会因此前进。
  link_checks 迁移了 88 行，序列却停在 1 → 每天巡检插入第一条就主键冲突失败，
  而**每次失败只让序列前进 1** → 整整 10 天巡检都在失败，
  而且按那个速度还要 62 天才能"自愈"。
"""

from __future__ import annotations

from app import migrate


class _FakeConn:
    """只记录收到过什么 SQL，不需要真数据库。"""

    def __init__(self):
        self.sql: list[str] = []
        self.committed = False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return self

    def fetchone(self):
        return {"v": 42}

    def commit(self):
        self.committed = True


def test_reset_sequences_covers_all_auto_id_tables():
    conn = _FakeConn()
    result = migrate._reset_sequences(conn)

    assert set(result) == set(migrate.AUTO_ID_TABLES), "每张自增表都要重置"
    assert conn.committed, "重置后要提交"

    for table in migrate.AUTO_ID_TABLES:
        joined = " ".join(conn.sql)
        assert f"pg_get_serial_sequence('{table}', 'id')" in joined, f"{table} 没重置序列"
        assert f"MAX(id) FROM {table}" in joined, f"{table} 没按实际最大 id 对齐"


def test_auto_id_tables_are_the_ones_with_serial_id():
    """自增表清单要和 TABLES 里带 id 主键的表一致 —— 不一致就会漏掉某张表。"""
    tables_with_id_pk = {name for name, pk, _ in migrate.TABLES if pk == ["id"]}
    assert tables_with_id_pk == set(migrate.AUTO_ID_TABLES)


def test_migrate_reports_missing_source(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        migrate.migrate(tmp_path / "not-there.db")
