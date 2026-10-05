from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, create_model

from app.agent.run_scope import current_scope
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.notes import english_instruction
from app.tools.registry import register

_ENTITY_HELP = "The watched entity whose state change triggers the task."


def _normalise(state: str) -> str:
    # "Segment cleaning", "segment-cleaning" and "segment_cleaning" are one state.
    return state.strip().lower().replace(" ", "_").replace("-", "_")


def resolve_states(requested: str, options: list[str]) -> list[str]:
    """Map what the model asked for onto the entity's real (raw) states:
    every option containing it, ignoring case/spaces/underscores. So
    "Segment cleaning" -> [segment_cleaning], and "cleaning" -> [cleaning,
    segment_cleaning, spot_cleaning, ...]. Empty when nothing fits. Resolved
    at save time, so the stored list is exactly what will fire."""
    wanted = _normalise(requested)
    matches = []
    for option in options:
        if wanted in _normalise(option):
            matches.append(option)
    return matches


class Params(BaseModel):
    entity_id: str = Field(description=_ENTITY_HELP)
    to_state: str | None = Field(
        None,
        description="Trigger only when the entity changes to this state, e.g. 'cleaning' "
        "or 'on'. A partial word matches every state containing it. Omit to trigger "
        "on any change.",
    )
    instruction: str = Field(description="What to do then, as one self-contained sentence.")
    kind: Literal["action", "reminder"] = Field(
        description="'reminder' = tell the user something; 'action' = make the house do something."
    )
    tags: list[str] = Field(default_factory=list, description="Optional short labels.")
    expires_in_hours: int | None = Field(
        None, description="Drop the task if it has not triggered after this many hours (default 24)."
    )


def dynamic_params(ctx) -> type[BaseModel]:
    """Offer the watched entities as an enum, so a small model picks the right
    id without a search first."""
    watched = list(ctx.settings.watched_entities)
    if not watched:
        return Params
    return create_model(
        "ScheduleOnStateChangeParams",
        __base__=Params,
        entity_id=(Literal[tuple(watched)], Field(description=_ENTITY_HELP)),
    )


async def handler(params: Params, ctx) -> ToolResult:
    watched = list(ctx.settings.watched_entities)
    if not watched:
        return ToolResult.error(
            "feature_disabled", "State-change scheduling is off: no watched entities are configured."
        )
    if ctx.notes is None:
        return ToolResult.error("notes_unavailable", "The note store is unavailable.")
    if params.entity_id not in watched:
        return ToolResult.error(
            "entity_not_watched",
            f"{params.entity_id!r} is not watched, so a task on it would never trigger.",
            data={"watched": watched},
        )

    # Enum sensors (e.g. a vacuum status) list their raw states in `options`;
    # the UI shows labels like "Segment cleaning" that would never match.
    states = []
    if params.to_state:
        state = await ctx.rest.get_state(params.entity_id)
        options = (state.get("attributes") or {}).get("options") or []
        if options:
            states = resolve_states(params.to_state, options)
            if not states:
                return ToolResult.error(
                    "invalid_state",
                    f"{params.to_state!r} is not a state of {params.entity_id}. "
                    "Use one of valid_states.",
                    data={"valid_states": list(options)},
                )
        else:
            states = [params.to_state]

    hours = params.expires_in_hours or ctx.settings.memory_note_default_ttl_hours
    hours = max(1, min(hours, ctx.settings.memory_note_max_ttl_hours))
    english, original = await english_instruction(ctx, params.instruction)
    scope = current_scope()
    now = datetime.now(timezone.utc)
    note_id = ctx.notes.add_state_note(
        entity_id=params.entity_id,
        to_state="|".join(states) if states else None,
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
        return ToolResult.error("notes_unavailable", "Could not save the task.")
    return ToolResult.ok({
        "id": note_id,
        "entity_id": params.entity_id,
        "to_state": states or None,
        "expires_in_hours": hours,
    })


register(
    ToolDefinition(
        name="schedule_on_state_change",
        description=(
            "Make the house react LATER, when a device changes state - use this instead of "
            "an automation. E.g. 'once the vacuum starts, stop it' -> the vacuum's "
            "entity_id, to_state='cleaning', kind='action'. When it happens you are called "
            "back and carry out `instruction` with your normal tools. Do not act now. For "
            "a clock time use schedule_at_time."
        ),
        params_model=Params,
        tier=Tier.READ,  # agent-local state only; never writes to HA
        handler=handler,
        dynamic_params=dynamic_params,
    )
)
