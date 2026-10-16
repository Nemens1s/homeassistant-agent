"""/api/notes: pending memory notes (scheduled tasks) for the settings UI."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _note_store(request: Request):
    notes = getattr(request.app.state, "notes", None)
    if notes is None:
        raise HTTPException(status_code=503, detail="note store unavailable")
    return notes


@router.get("/api/notes")
async def list_notes(request: Request, status: str = "pending") -> dict:
    notes = _note_store(request)
    if status == "all":
        rows = notes.list_recent(limit=100)
    else:
        rows = notes.list_pending(datetime.now(timezone.utc))
    result = []
    for note in rows:
        result.append(note.to_dict())
    return {"notes": result}


@router.delete("/api/notes/{note_id}")
async def delete_note(note_id: int, request: Request) -> dict:
    notes = _note_store(request)
    if not notes.cancel(note_id):
        raise HTTPException(status_code=404, detail="no pending note with that id")
    return {"ok": True}
