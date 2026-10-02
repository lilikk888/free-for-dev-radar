"""精选条目的复核流程。

## 为什么需要「流程」而不只是一个日期字段

精选这层数据靠人工维护，而**人工维护的东西一定会腐烂**：
自动抓的那层有采集任务盯着、链接挂了有巡检盯着，
唯独「免费额度从 2000 万缩成 1000 万」这类变化**没有任何机制能自动发现**。

所以这个模块不假装能检测政策变化，只做一件实事：
**把「该复核了」变成一个可以执行的清单** —— 每条都告诉你具体要核对哪几项，
并给官网直达链接。复核成本低到"顺手点一下"，这件事才有机会真的发生。

## 两个真相来源（关键设计）

| | 存在哪 | 谁写 | 回答什么 |
|---|---|---|---|
| `data_curated.reviewed` | 代码 | 改数据的人 | 「这版数据是什么时候写的」 |
| `review_log.confirmed_at` | 数据库 | 用的时候顺手点 | 「最近一次确认它还准是什么时候」 |

算新鲜度时取两者中**较新的**。这样：
- 我改了内容 → 更新代码里的 `reviewed`
- 你只是点开看了一圈觉得没变 → 点「确认还准」，不用改代码
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from . import data_curated, db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def confirm(entry_id: str, note: str | None = None) -> dict[str, Any]:
    """标记「这条我核对过了，当前还准」。

    幂等：重复确认只是刷新时间。
    """
    from . import userdata

    if userdata.get(entry_id) is None and entry_id not in {e["id"] for e in data_curated.as_list()}:
        # 只允许确认精选条目 —— 否则会攒一堆对不上的孤儿记录
        raise ValueError(f"没有这条精选条目: {entry_id!r}")

    ts = _now()
    conn = db.connect()
    try:
        conn.execute(
            """INSERT INTO review_log (entry_id, confirmed_at, note)
                   VALUES (?, ?, ?)
               ON CONFLICT (entry_id) DO UPDATE
                   SET confirmed_at = excluded.confirmed_at,
                       note = excluded.note""",
            (entry_id, ts, note),
        )
        conn.commit()
    finally:
        conn.close()
    return {"entry_id": entry_id, "confirmed_at": ts, "note": note}


def forget(entry_id: str) -> bool:
    """撤销一次确认（比如点错了）。"""
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM review_log WHERE entry_id = ?", (entry_id,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def confirmations() -> dict[str, dict[str, Any]]:
    """全部确认记录：entry_id -> {confirmed_at, note}。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT entry_id, confirmed_at, note FROM review_log"
        ).fetchall()
    finally:
        conn.close()
    return {r["entry_id"]: {"confirmed_at": r["confirmed_at"], "note": r["note"]} for r in rows}


def checkpoints(entry: dict[str, Any]) -> list[str]:
    """这条要核对哪几项 —— 把"去复核"拆成能照着做的小任务。

    刻意从**已有的字段**推导，而不是让复核人自己想该看什么：
    点开官网之后如果不知道要对什么，复核就变成了走过场。
    """
    out: list[str] = []
    if entry.get("quota"):
        out.append(f"额度是否仍是「{entry['quota']}」")
    if entry.get("expiry"):
        out.append(f"有效期是否仍是「{entry['expiry']}」")
    reqs = entry.get("requirements") or []
    if "免信用卡" in reqs:
        out.append("是否还免信用卡（有没有偷偷加绑卡要求）")
    elif "需绑卡" in reqs:
        out.append("是否仍需绑卡")
    if "需实名" in reqs:
        out.append("是否需要实名")
    if entry.get("china") == "cn_ok":
        out.append("国内是否仍能直连（有没有被墙）")
    out.append("官网上这个活动/免费档还在不在")
    return out


def statuses(today: date | None = None) -> dict[str, dict[str, Any]]:
    """每条精选的综合新鲜度（合并代码里的 reviewed 与数据库里的 confirmed_at）。"""
    today = today or date.today()
    conf = confirmations()
    out: dict[str, dict[str, Any]] = {}
    for e in data_curated.as_list():
        c = conf.get(e["id"], {})
        st = data_curated.review_status(e.get("reviewed"), c.get("confirmed_at"), today)
        st["note"] = c.get("note")
        out[e["id"]] = st
    return out


def checklist(today: date | None = None) -> dict[str, Any]:
    """待复核清单：最久没核对的排前面，每条附「要核对什么」+ 官网链接。

    没有这条清单的话，「定期复核」就是个空口号 ——
    人打开 88 条数据不知道从哪下手，最后就是不复核。
    """
    today = today or date.today()
    st_all = statuses(today)
    pending: list[dict[str, Any]] = []
    for e in data_curated.as_list():
        st = st_all[e["id"]]
        if not st["needs_review"]:
            continue
        pending.append({
            "id": e["id"],
            "name": e["name"],
            "category_cn": data_curated.category_name(e.get("category", "")),
            "url": e.get("url"),
            "quota": e.get("quota"),
            "reviewed": st.get("reviewed"),
            "confirmed_at": st.get("confirmed_at"),
            "age_months": st.get("review_age_months"),
            "checkpoints": checkpoints(e),
        })
    # 最久的排最前；没有年龄信息的（从没核对过）排最最前
    pending.sort(key=lambda x: (-1 if x["age_months"] is None else -x["age_months"], x["name"]))

    return {
        "total": len(st_all),
        "pending": len(pending),
        "items": pending,
        # 顺便回报下次该什么时候看，避免"复核完就忘了"
        "interval_months": data_curated.REVIEW_INTERVAL_MONTHS,
    }
