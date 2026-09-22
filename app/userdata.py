"""「我的领用记录」——用户对精选条目的个人状态。

为什么这个是整个项目最有用的功能：

国内平台的免费额度**几乎都是限时的**（智谱 3 个月、百炼 90 天、混元 1 年、
硅基流动 180 天……）。真实场景是：一次领了七八家，过两个月全忘了，
额度到期白白浪费。所以「记下来 + 到期前提醒」比「多看几个清单」有用得多。

设计取舍：
- 用 SQLite 单表存（跟快照同一个库），不引入额外依赖
- `expires_at` 存 ISO 日期字符串，查询用字符串比较即可（ISO 格式天然可比）
- 到期提醒**不自己发邮件**，而是暴露成 Prometheus 指标 →
  复用已经验证过的 Alertmanager → 163 邮件通道。
  好处：零新增凭据、零新增发信代码路径、且能看到历史曲线。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from . import data_curated
from . import db

VALID_STATUS = ("interested", "registered", "dropped")
STATUS_CN = {
    "interested": "想试试",
    "registered": "已注册",
    "dropped": "已放弃",
}

# ⚠️ 必须用正则卡死格式。曾经只靠 date.fromisoformat() 校验，
# 结果 "20260901" 也能通过（Python 3.11+ 接受无分隔符写法），
# 数据库里就混进了两种格式，字符串比较排序随即失效。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(
    entry_id: str,
    *,
    status: str = "interested",
    expires_at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """新建或更新一条领用记录。"""
    if status not in VALID_STATUS:
        raise ValueError(f"status 必须是 {VALID_STATUS} 之一，收到 {status!r}")
    if expires_at:
        # 只接受严格的 YYYY-MM-DD —— 库里必须只有一种格式，
        # 否则字符串比较（排序、范围查询）会给出错误结果。
        if not _DATE_RE.match(expires_at):
            raise ValueError("expires_at 必须是 YYYY-MM-DD 格式（例如 2026-10-02）")
        try:
            date.fromisoformat(expires_at)
        except ValueError as exc:
            raise ValueError(f"expires_at 不是合法日期: {expires_at}") from exc

    conn = db.connect()
    try:
        existing = conn.execute(
            "SELECT registered_at FROM mine WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        registered_at = existing["registered_at"] if existing else None
        if status == "registered" and not registered_at:
            registered_at = date.today().isoformat()

        conn.execute(
            """
            INSERT INTO mine (entry_id, status, registered_at, expires_at, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                status        = excluded.status,
                registered_at = COALESCE(excluded.registered_at, mine.registered_at),
                expires_at    = excluded.expires_at,
                note          = excluded.note,
                updated_at    = excluded.updated_at
            """,
            (entry_id, status, registered_at, expires_at, note, _now()),
        )
        conn.commit()
        return get(entry_id) or {}
    finally:
        conn.close()


def delete(entry_id: str) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM mine WHERE entry_id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get(entry_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM mine WHERE entry_id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_all() -> list[dict[str, Any]]:
    """我的清单，按到期日排序（没填到期日的排最后）。

    会顺带把精选数据里的**服务名和链接**补进来 ——
    数据库里只存了 entry_id，不加这一步，界面和邮件里就只能显示
    「zhipu」这种 ID，用户看不懂（实测踩过）。
    """
    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM mine ORDER BY (expires_at IS NULL), expires_at ASC, updated_at DESC"
            )
        ]
    finally:
        conn.close()

    index = {e["id"]: e for e in data_curated.as_list()}
    for r in rows:
        meta = index.get(r["entry_id"], {})
        r["name"] = meta.get("name", r["entry_id"])
        r["url"] = meta.get("url")
        r["category"] = meta.get("category")
        r["category_cn"] = data_curated.category_name(meta["category"]) if meta.get("category") else None
        r["quota"] = meta.get("quota")
        r["known"] = bool(meta)  # False = 这条精选已从数据里删掉，界面上要能看出来
        r["status_cn"] = STATUS_CN.get(r["status"], r["status"])
        r["days_left"] = days_left(r.get("expires_at"))
    return rows


def days_left(expires_at: str | None) -> int | None:
    """距离到期还有多少天（负数=已过期，None=没填到期日）。"""
    if not expires_at:
        return None
    try:
        return (date.fromisoformat(expires_at) - date.today()).days
    except ValueError:
        return None


def expiring(within_days: int = 14) -> list[dict[str, Any]]:
    """即将到期（含已过期）的记录。

    只统计「已注册」的 —— 没注册的额度不存在过期问题，提醒它没意义。
    """
    today = date.today()
    out = []
    for r in list_all():
        if r["status"] != "registered":
            continue
        d = r["days_left"]
        if d is None or d > within_days:
            continue
        out.append(r)
    out.sort(key=lambda x: x["days_left"])
    return out


def stats() -> dict[str, int]:
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM mine").fetchone()["c"]
        by = {
            r["status"]: r["c"]
            for r in conn.execute("SELECT status, COUNT(*) AS c FROM mine GROUP BY status")
        }
    finally:
        conn.close()
    today = date.today()
    soon = sum(
        1
        for r in list_all()
        if r["status"] == "registered"
        and r["days_left"] is not None
        and 0 <= r["days_left"] <= 14
    )
    expired = sum(
        1
        for r in list_all()
        if r["status"] == "registered" and r["days_left"] is not None and r["days_left"] < 0
    )
    return {
        "total": total,
        "registered": by.get("registered", 0),
        "interested": by.get("interested", 0),
        "dropped": by.get("dropped", 0),
        "expiring_14d": soon,
        "expired": expired,
        "_today": (today - today).days,  # 占位，保持返回结构稳定
    }
