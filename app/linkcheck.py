"""链接巡检：定时检查精选条目里的官网还能不能打开。

为什么值得单独做一个功能：
免费服务最容易"悄悄死掉" —— 被收购、免费档取消、官网改版。清单里链接一挂，
用户点进去看到 404，下次就不信这个工具了。与其等用户发现，不如自己定时巡一遍。

设计取舍：
- **只检查精选条目**（88 条），不检查 free-for-dev 那 1300 条
  （那是别人的清单，我们只负责自己人工核实过的那部分）
- 结果**追加**存库，不覆盖 —— 这样能回答"这条链接是从哪天开始挂的"
- 先试 HEAD，很多站点不支持 HEAD（405），再退回 GET
- 并发用线程池，88 个站点串行会跑很久；并发 8 是个不激进的折中
- **不因为巡检失败就让 CronJob 失败**：单个站点挂了很正常，
  整体结果写进指标，由 Prometheus 告警来触发关注
"""

from __future__ import annotations

import concurrent.futures
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import data_curated
from . import db

USER_AGENT = "free-for-dev-radar-linkcheck/1.0 (+https://github.com/lilikk888/free-for-dev-radar)"
TIMEOUT = 15
WORKERS = 8

# ⭐ 判定规则：**「站点还在」和「能自动打开」是两件事**。
# 第一次跑真实巡检时踩到：
#   - Mistral 返回 307（重定向）被当成挂了
#   - 华为云返回 418（反爬的玩笑码）被当成挂了
# 这些站点对用户都是好的。所以规则改成：
#   2xx / 3xx                    → 正常
#   401 / 403 / 405 / 418 / 429  → 正常（站点在，只是拒绝自动访问）
#   404 / 410 / 5xx / 网络异常    → 判定为不可达（真的可能停服了）
REACHABLE_ANYWAY = {401, 403, 405, 418, 429}
GONE = {404, 410, 451}


def _classify(status: int) -> bool:
    if 200 <= status < 400:
        return True
    if status in REACHABLE_ANYWAY:
        return True
    return False


def _probe(url: str) -> tuple[bool, int | None, str | None]:
    """探测单个 URL。返回 (是否正常, 状态码, 错误信息)。

    网络层面的失败（超时/SSL/连接重置）会**重试一次** ——
    跨云跨境的网络抖动很常见，一次失败就判死会产生大量误报。
    """
    last_error: str | None = None

    for attempt in range(2):
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    return True, resp.status, None
            except urllib.error.HTTPError as exc:
                if _classify(exc.code):
                    note = f"HTTP {exc.code}（站点可达，拒绝或重定向自动访问）"
                    return True, exc.code, note
                if exc.code in GONE:
                    return False, exc.code, f"HTTP {exc.code}（页面不存在）"
                if method == "HEAD":
                    continue  # 有些站点不支持 HEAD，换 GET
                return False, exc.code, f"HTTP {exc.code}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:200]
                if method == "HEAD":
                    continue
        # 网络层失败：等一秒重试一次
        time.sleep(1)

    return False, None, last_error or "HEAD 与 GET 都失败"


def check_all(limit: int | None = None) -> dict[str, Any]:
    """巡检全部精选条目，把结果写库。返回汇总。"""
    entries = data_curated.as_list()
    if limit:
        entries = entries[:limit]

    results: list[dict[str, Any]] = []

    def run(e: dict) -> dict:
        ok, code, err = _probe(e["url"])
        return {"entry_id": e["id"], "name": e["name"], "url": e["url"],
                "ok": ok, "status_code": code, "error": err}

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for r in pool.map(run, entries):
            results.append(r)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db.connect()
    try:
        conn.executemany(
            "INSERT INTO link_checks (entry_id, checked_at, ok, status_code, error) VALUES (?, ?, ?, ?, ?)",
            [(r["entry_id"], now, 1 if r["ok"] else 0, r["status_code"], r["error"]) for r in results],
        )
        conn.commit()
    finally:
        conn.close()

    dead = [r for r in results if not r["ok"]]
    dead.sort(key=lambda r: r["name"])
    return {
        "checked_at": now,
        "total": len(results),
        "ok": len(results) - len(dead),
        "dead": len(dead),
        "dead_entries": dead,
    }


def latest() -> dict[str, dict[str, Any]]:
    """每条条目最近一次的巡检结果。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT lc.entry_id, lc.checked_at, lc.ok, lc.status_code, lc.error
            FROM link_checks lc
            JOIN (SELECT entry_id, MAX(id) AS mid FROM link_checks GROUP BY entry_id) t
              ON t.entry_id = lc.entry_id AND t.mid = lc.id
            """
        ).fetchall()
    finally:
        conn.close()
    return {r["entry_id"]: dict(r) for r in rows}


def sync_seen() -> list[str]:
    """把当前精选条目记进 seen_entries，返回**本次首次见到**的条目 id。

    每周订阅邮件里的「本周新收录」就靠它算出来 ——
    这样不用手工维护 changelog，数据一加就自动算出来。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = [e["id"] for e in data_curated.as_list()]
    conn = db.connect()
    try:
        known = {r["entry_id"] for r in conn.execute("SELECT entry_id FROM seen_entries")}
        fresh = [i for i in ids if i not in known]
        conn.executemany(
            "INSERT INTO seen_entries (entry_id, first_seen, last_seen) VALUES (?, ?, ?)",
            [(i, now, now) for i in fresh],
        )
        conn.executemany(
            "UPDATE seen_entries SET last_seen = ? WHERE entry_id = ?",
            [(now, i) for i in ids if i in known],
        )
        conn.commit()
    finally:
        conn.close()
    return fresh


def newly_seen(since_days: int = 7) -> list[dict[str, Any]]:
    """最近 N 天内首次出现的精选条目。"""
    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT entry_id, first_seen FROM seen_entries ORDER BY first_seen DESC"
            )
        ]
    finally:
        conn.close()

    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    fresh_ids = [r["entry_id"] for r in rows if r["first_seen"][:10] >= cutoff]
    index = {e["id"]: e for e in data_curated.as_list()}
    return [index[i] for i in fresh_ids if i in index]


def stats() -> dict[str, int]:
    conn = db.connect()
    try:
        last = conn.execute("SELECT MAX(checked_at) AS t FROM link_checks").fetchone()["t"]
        if not last:
            return {"checked": 0, "dead": 0}
        row = conn.execute(
            "SELECT COUNT(*) AS c, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS d "
            "FROM link_checks WHERE checked_at = ?",
            (last,),
        ).fetchone()
        return {"checked": row["c"], "dead": row["d"] or 0, "checked_at": last}
    finally:
        conn.close()
