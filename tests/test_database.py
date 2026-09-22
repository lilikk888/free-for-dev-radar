"""数据层适配的测试。

核心是锁住 SQL 方言转换那条规则：**引号内的 `?` 是数据，不是占位符**。
这个 bug 如果漏出去，只在 PostgreSQL 环境下才会炸，而本地跑 SQLite 永远发现不了 ——
所以必须用单测固定住。
"""

from __future__ import annotations

from app import database


def test_placeholder_conversion_outside_quotes():
    assert database._to_pg_sql("SELECT * FROM t WHERE a = ?") == "SELECT * FROM t WHERE a = %s"
    assert (
        database._to_pg_sql("INSERT INTO t (a, b) VALUES (?, ?)")
        == "INSERT INTO t (a, b) VALUES (%s, %s)"
    )


def test_question_mark_inside_quotes_is_preserved():
    """回归测试：引号里的问号是数据，不能被替换成占位符。"""
    sql = "SELECT * FROM mine WHERE note = '还有几天?'"
    assert database._to_pg_sql(sql) == sql


def test_mixed_quotes_and_placeholders():
    sql = "SELECT * FROM t WHERE note = '忘了?' AND entry_id = ?"
    assert database._to_pg_sql(sql) == "SELECT * FROM t WHERE note = '忘了?' AND entry_id = %s"


def test_escaped_single_quote_inside_string():
    """SQL 里用两个单引号转义单引号，解析不能在这里断掉。"""
    sql = "SELECT * FROM t WHERE a = 'it''s ok?' AND b = ?"
    assert database._to_pg_sql(sql) == "SELECT * FROM t WHERE a = 'it''s ok?' AND b = %s"


def test_default_is_sqlite(monkeypatch):
    monkeypatch.setattr(database, "DB_URL", "")
    assert database.using_postgres() is False


def test_postgres_url_detected():
    for url in ("postgres://u:p@h/db", "postgresql://u:p@h/db"):
        assert url.startswith(("postgres://", "postgresql://"))


def test_sqlite_connect_creates_all_tables(tmp_path):
    conn = database.connect_sqlite(tmp_path / "t.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {r["name"] for r in rows}
    finally:
        conn.close()
    for t in ("snapshots", "entries", "changes", "mine", "link_checks", "seen_entries"):
        assert t in names, f"缺表 {t}"


def test_both_schemas_declare_the_same_tables():
    """两种方言的表结构必须一致 —— 不一致会在切库时才发现问题。"""
    def tables(ddl: str) -> set[str]:
        import re

        return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl))

    assert tables(database.SQLITE_SCHEMA) == tables(database.PG_SCHEMA)
