"""只读 HTTP 层：给页面用的 JSON API + 静态首页 + Prometheus 指标端点。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse

from . import db
from . import metrics as metrics_mod

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="free-for-dev radar", version="0.1.0")

# 已知路由的固定清单。指标标签必须是有界的，否则随便一个扫描器
# 打一堆 /xxx 进来就能把 Prometheus 的标签基数打爆。
_KNOWN_PATHS = ("/", "/healthz", "/metrics", "/api/changes", "/api/snapshots", "/api/stats")


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
    return FileResponse(STATIC_DIR / "index.html")
