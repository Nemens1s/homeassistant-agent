"""/api/telemetry/* and /api/labels: read the local telemetry DB and record
user feedback on a request. All return 503 when telemetry is disabled."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.telemetry.store import insert_label, recent_requests, summary

router = APIRouter()


class LabelRequest(BaseModel):
    request_id: str
    source: str
    rating: int | None = None
    correct_tool: str | None = None
    correct_entity_id: str | None = None
    note: str | None = None


def _store(request: Request):
    conn = getattr(request.app.state, "telemetry_store", None)
    if conn is None:
        raise HTTPException(status_code=503, detail="telemetry disabled")
    return conn


@router.get("/api/telemetry/summary")
async def get_telemetry_summary(request: Request, days: int = 7) -> dict:
    return summary(_store(request), days=days)


@router.get("/api/telemetry/requests")
async def get_telemetry_requests(request: Request, limit: int = 20,
                                 cursor: str | None = None) -> dict:
    return recent_requests(_store(request), limit=limit, cursor=cursor)


@router.post("/api/labels")
async def post_label(req: LabelRequest, request: Request) -> dict:
    label_id = insert_label(
        _store(request),
        request_id=req.request_id,
        source=req.source,
        rating=req.rating,
        correct_tool=req.correct_tool,
        correct_entity_id=req.correct_entity_id,
        note=req.note,
    )
    return {"id": label_id}
