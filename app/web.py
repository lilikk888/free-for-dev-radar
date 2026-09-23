"""只读 HTTP 层：中文检索 API + 静态首页 + Prometheus 指标端点。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import data_curated
from . import db
from . import metrics as metrics_mod
from . import search as search_mod
from . import tracing
from . import userdata

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="免费资源雷达", version="0.3.0")

# 链路追踪（可选）：没配 OTEL_EXPORTER_OTLP_ENDPOINT 就什么都不做
tracing.setup(app)

# 已知路由的固定清单。指标标签必须是有界的，否则随便一个扫描器
# 打一堆 /xxx 进来就能把 Prometheus 的标签基数打爆。
_KNOWN_PATHS = (
    "/", "/healthz", "/metrics", "/api/changes", "/api/snapshots", "/api/stats",
    "/api/search", "/api/categories", "/api/curated", "/api/tags",
)


def _foreign_rows() -> list[dict]:
    """取最近一次快照里的全部条目（免费清单的原始数据）。"""
    conn = db.connect()
    try:
        latest = conn.execute("SELECT MAX(id) AS id FROM snapshots").fetchone()["id"]
        if latest is None:
            return []
        return [
            dict(row)
            for row in conn.execute(
                "SELECT entry_key, category, name, url, description FROM entries WHERE snapshot_id = ?",
                (latest,),
            )
        ]
    finally:
        conn.close()


def _path_label(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    raw = request.url.path
    return raw if raw in _KNOWN_PATHS else "other"


@app.middleware("http")
async def count_requests(request: Request, call_next):
    response = await call_next(request)
    metrics_mod.HTTP_REQUESTS.labels(
        request.method, _path_label(request), str(response.status_code)
    ).inc()
    return response


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """给 Prometheus 抓的端点。

    ⚠️ 当前应用的 Ingress 是 catch-all（无 host 的 `/`），所以这个路径
    公网也能访问。指标本身不含敏感信息，但生产环境的正确做法是二选一：
      1. 把 metrics 挪到单独端口，只让 Service 暴露、Ingress 不路由
      2. 用 NetworkPolicy 只放行 monitoring 命名空间（Calico 支持）
    """
    payload, content_type = metrics_mod.render_web_metrics()
    return Response(content=payload, media_type=content_type)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/search")
def api_search(
    q: str = Query("", description="中文需求，如「免费的大模型 API」「图床」"),
    cat: str | None = Query(None, description="按精选分类过滤"),
    tags: str | None = Query(None, description="逗号分隔的标签，如 need_id,cn_ok"),
    limit: int = Query(60, ge=1, le=300),
) -> dict[str, Any]:
    """核心接口：把中文需求翻译成检索条件，返回精选 + 收录两层结果。"""
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    result = search_mod.search(
        query=q,
        cat=cat,
        tags=tag_list,
        foreign_rows=_foreign_rows(),
        limit=limit,
    )
    # 把最近一次链接巡检结果并进结果里，页面上能给「官网挂了」的条目打标记
    try:
        from . import linkcheck

        latest = linkcheck.latest()
        for item in result["curated"]:
            v = latest.get(item["id"])
            item["link_ok"] = None if v is None else bool(v["ok"])
            item["link_error"] = (v or {}).get("error")
    except Exception:  # pragma: no cover - 巡检还没跑过时不影响搜索
        pass
    return result


@app.get("/api/categories")
def api_categories(q: str = Query("", description="给定时按相关度排序")) -> dict[str, Any]:
    """精选分类清单（带条目数），首页的分类入口用这个渲染。"""
    counts: dict[str, int] = {}
    for e in data_curated.as_list():
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    guessed = search_mod.guess_categories(q) if q else []
    cats = []
    for key, name in data_curated.CATEGORIES:
        cats.append({"key": key, "name": name, "count": counts.get(key, 0)})
    if guessed:
        order = {k: i for i, k in enumerate(guessed)}
        cats.sort(key=lambda c: order.get(c["key"], 999))
    return {"categories": cats, "guessed": guessed}


class MinePayload(BaseModel):
    """标记我的领用状态。"""

    status: str = Field("registered", description="interested / registered / dropped")
    expires_at: str | None = Field(None, description="额度到期日 YYYY-MM-DD")
    note: str | None = Field(None, description="备注，比如用了哪个模型")


@app.get("/api/mine")
def api_mine_list() -> dict[str, Any]:
    """我的清单：已注册 / 想试试 / 快到期。"""
    return {"items": userdata.list_all(), "stats": userdata.stats()}


@app.put("/api/mine/{entry_id}")
def api_mine_put(entry_id: str, payload: MinePayload) -> dict[str, Any]:
    """新建或更新一条领用记录（页面上的「我注册了」按钮打的就是这个）。"""
    try:
        item = userdata.upsert(
            entry_id,
            status=payload.status,
            expires_at=payload.expires_at,
            note=payload.note,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "item": item, "days_left": userdata.days_left(item.get("expires_at"))}


@app.delete("/api/mine/{entry_id}")
def api_mine_delete(entry_id: str) -> dict[str, Any]:
    return {"ok": userdata.delete(entry_id)}


@app.get("/api/mine/expiring")
def api_mine_expiring(days: int = Query(14, ge=1, le=365)) -> dict[str, Any]:
    """即将到期（含已过期）的额度 —— 邮件提醒看的就是这份数据。"""
    items = userdata.expiring(within_days=days)
    return {"days": days, "count": len(items), "items": items}


@app.get("/api/links")
def api_links(only_dead: bool = Query(False, description="只看不可达的")) -> dict[str, Any]:
    """链接巡检结果（页面上的「链接可能失效」标记就读这个）。"""
    from . import linkcheck

    latest = linkcheck.latest()
    items = [v for v in latest.values() if (not only_dead or not v["ok"])]
    return {"stats": linkcheck.stats(), "items": items}


@app.get("/api/curated")
def api_curated(cat: str | None = None) -> dict[str, Any]:
    """全部人工精选条目（供页面做客户端筛选/收藏）。"""
    items = [e for e in data_curated.as_list() if not cat or e["category"] == cat]
    return {"count": len(items), "items": items}


@app.get("/api/tags")
def api_tags() -> dict[str, Any]:
    """可用标签及其含义（页面上给用户解释「需实名」「国内直连」是什么意思）。"""
    return {
        "tags": [
            {"key": "no_card", "name": "免信用卡", "desc": "注册不需要绑银行卡"},
            {"key": "need_card", "name": "需绑卡", "desc": "要绑卡验证（多数不扣费，但务必留意）"},
            {"key": "need_id", "name": "需实名", "desc": "需要实名认证（国内平台基本都有）"},
            {"key": "cn_ok", "name": "国内直连", "desc": "国内网络可直接访问，无需梯子"},
            {"key": "cn_partial", "name": "部分可用", "desc": "能用但速度一般，或部分功能受限"},
            {"key": "cn_no", "name": "需梯子", "desc": "国内直接访问困难"},
            {"key": "permanent", "name": "长期免费", "desc": "不是限时赠送，可长期使用"},
        ]
    }


@app.get("/api/changes")
def list_changes(
    limit: int = Query(100, ge=1, le=1000),
    change_type: str | None = Query(None, description="added / removed / updated"),
) -> dict[str, Any]:
    conn = db.connect()
    try:
        sql = "SELECT * FROM changes"
        params: list[Any] = []
        if change_type:
            sql += " WHERE change_type = ?"
            params.append(change_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        items = [dict(row) for row in conn.execute(sql, params)]
        return {"count": len(items), "items": items}
    finally:
        conn.close()


@app.get("/api/snapshots")
def list_snapshots(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    conn = db.connect()
    try:
        items = [
            dict(row)
            for row in conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,))
        ]
        return {"count": len(items), "items": items}
    finally:
        conn.close()


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    conn = db.connect()
    try:
        latest = conn.execute("SELECT MAX(id) AS id FROM snapshots").fetchone()["id"]
        entry_count = 0
        if latest is not None:
            entry_count = conn.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE snapshot_id = ?", (latest,)
            ).fetchone()["c"]

        by_type = {
            row["change_type"]: row["c"]
            for row in conn.execute(
                "SELECT change_type, COUNT(*) AS c FROM changes GROUP BY change_type"
            )
        }
        last_snapshot = conn.execute(
            "SELECT fetched_at, entry_count FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "current_entries": entry_count,
            "changes_by_type": by_type,
            "last_snapshot": dict(last_snapshot) if last_snapshot else None,
        }
    finally:
        conn.close()


@app.get("/")
def index() -> FileResponse:
    """首页：中文需求检索（「我需要什么 → 有没有免费的」）。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/changes")
def changes_page() -> FileResponse:
    """次要页面：上游清单的变更历史（原「变更雷达」视图，保留作为附属能力）。"""
    return FileResponse(STATIC_DIR / "changes.html")
