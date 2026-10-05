"""Shared by the memory tools: English normalisation and the compact row the
agent sees for a note."""

from __future__ import annotations

from app.events.clock import format_clock, from_key


async def english_instruction(ctx, text: str) -> tuple[str, str | None]:
    """The instruction as stored (always English) plus the user's original
    wording when translation changed it. Small models and the fast path only
    ever see English."""
    english, _source = await ctx.lang.to_english(text)
    if english == text:
        return text, None
    return english, text


def note_summary(note) -> dict:
    row = {"id": note.id, "kind": note.kind, "instruction": note.instruction}
    if note.trigger_kind == "time":
        row["when"] = format_clock(from_key(note.fire_at_local))
    else:
        states = (note.to_state or "any change").replace("|", " or ")
        row["when"] = f"{note.entity_id} → {states}"
        row["expires_at"] = note.expires_at
    return row
