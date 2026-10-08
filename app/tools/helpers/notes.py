"""Shared by the memory tools: English normalisation and the compact row the
agent sees for a note."""

from __future__ import annotations

from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX
from app.events.clock import format_clock, from_key
from app.tools.helpers.actions import check_action


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
    if note.action_entity_id:
        row["action"] = note.action_entity_id
    if note.reminder:
        row["reminder"] = note.reminder
    return row


def task_kind(action_entity_id: str | None, reminder: str | None) -> str:
    """Display label: what firing will do. 'agent' = LLM fallback."""
    if action_entity_id and reminder:
        return "both"
    if action_entity_id:
        return "action"
    if reminder:
        return "reminder"
    return "agent"


async def _ai_action_ids(ctx) -> list[str]:
    ids = []
    for state in await ctx.rest.list_states():
        entity_id = state["entity_id"]
        if not entity_id.startswith((AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX)):
            continue
        if entity_id == ctx.settings.notify_action:
            continue
        ids.append(entity_id)
    return ids


async def require_action_or_reminder(ctx, params):
    """A task must say what happens when it fires. Small models skip optional
    fields, so 'neither' is refused - with the valid actions in front of the
    model - unless it explicitly opts into the LLM fallback."""
    if params.action_entity_id or params.reminder or params.no_single_action:
        return None
    from app.tools.base import ToolResult

    return ToolResult.error(
        "action_or_reminder_required",
        "Say what happens then: pick action_entity_id from valid_actions, or put the "
        "message for the user in reminder. Only if no single action fits, set "
        "no_single_action=true.",
        data={"valid_actions": await _ai_action_ids(ctx)},
    )


async def validated_action(ctx, entity_id: str, params: dict):
    """check_action plus the list of valid choices, so a wrong pick made while
    scheduling is corrected in the same conversation, not discovered at 18:00."""
    error = await check_action(ctx, entity_id, params)
    if error is None:
        return None
    data = dict(error.data or {})
    data["valid_actions"] = await _ai_action_ids(ctx)
    error.data = data
    return error
