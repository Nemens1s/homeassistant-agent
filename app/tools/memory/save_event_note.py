from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, create_model

from app.agent.run_scope import current_scope
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.notes import english_instruction
from app.tools.registry import register

_ENTITY_HELP = "The watched entity whose state change fires the note."


class Params(BaseModel):
    entity_id: str = Field(description=_ENTITY_HELP)
    to_state: str | None = Field(
        None,
        description="Fire only when the entity changes to this state, e.g. 'cleaning' "
        "or 'on'. Omit to fire on any change.",
    )
    instruction: str = Field(description="What to do then, as one self-contained sentence.")
    kind: Literal["action", "reminder"] = Field(
        description="'reminder' = tell the user something; 'action' = make the house do something."
    )
    tags: list[str] = Field(default_factory=list, description="Optional short labels.")
    expires_in_hours: int | None = Field(
        None, description="Forget the note after this many hours (default 24)."
    )


def dynamic_params(ctx) -> type[BaseModel]:
    """Offer the watched entities as an enum, so a small model picks the right
    id without a search first."""
    watched = list(ctx.settings.watched_entities)
    if not watched:
        return Params
    return create_model(
        "SaveEventNoteParams",
        __base__=Params,
        entity_id=(Literal[tuple(watched)], Field(description=_ENTITY_HELP)),
    )


async def handler(params: Params, ctx) -> ToolResult:
    watched = list(ctx.settings.watched_entities)
    if not watched:
        return ToolResult.error(
            "feature_disabled", "Event notes are off: no watched entities are configured."
        )
    if ctx.notes is None:
        return ToolResult.error("notes_unavailable", "The note store is unavailable.")
    if params.entity_id not in watched:
        return ToolResult.error(
            "entity_not_watched",
            f"{params.entity_id!r} is not watched, so a note on it would never fire.",
            data={"watched": watched},
        )

    hours = params.expires_in_hours or ctx.settings.memory_note_default_ttl_hours
    hours = max(1, min(hours, ctx.settings.memory_note_max_ttl_hours))
    english, original = await english_instruction(ctx, params.instruction)
    scope = current_scope()
    now = datetime.now(timezone.utc)
    note_id = ctx.notes.add_state_note(
        entity_id=params.entity_id,
        to_state=params.to_state,
        instruction=english,
        instruction_original=original,
        kind=params.kind,
        language=scope.language,
        tags=params.tags,
        expires_at=now + timedelta(hours=hours),
        now=now,
        source_thread_id=scope.thread_id,
    )
    if note_id is None:
        return ToolResult.error("notes_unavailable", "Could not save the note.")
    return ToolResult.ok({
        "id": note_id,
        "entity_id": params.entity_id,
        "to_state": params.to_state,
        "expires_in_hours": hours,
    })


register(
    ToolDefinition(
        name="save_event_note",
        description=(
            "Save a one-shot note to act on LATER, when a watched device changes state - "
            "e.g. 'once the vacuum starts, stop it' -> the vacuum's entity_id, "
            "to_state='cleaning', kind='action'. Use for 'when/once/after X happens' "
            "requests instead of acting now. For a clock time use save_time_note."
        ),
        params_model=Params,
        tier=Tier.READ,  # agent-local state only; never writes to HA
        handler=handler,
        dynamic_params=dynamic_params,
    )
)
