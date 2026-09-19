"""只读 HTTP 层：给页面用的 JSON API + 静态首页。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from . import db

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="free-for-dev radar", version="0.1.0")


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
