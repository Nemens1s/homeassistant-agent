from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field

from app.agent.run_scope import current_scope
from app.events.clock import FireTimeError, current_local_time, format_clock, resolve_fire_at
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.notes import (
    english_instruction,
    require_action_or_reminder,
    task_kind,
    validated_action,
)
from app.tools.registry import register


class Params(BaseModel):
    at: str | None = Field(
        None, description="Local time 'HH:MM' (next occurrence) or 'HH:MM DD-MM-YYYY'."
    )
    in_minutes: int | None = Field(
        None, description="Minutes from now, for 'in 20 minutes'. Use instead of 'at'."
    )
    instruction: str = Field(description="What to do then, as one self-contained sentence.")
    action_entity_id: str | None = Field(
        None,
        description="The action from list_actions to run then (as for trigger_action). "
        "Omit if no single action does it.",
    )
    action_params: dict = Field(
        default_factory=dict, description="Arguments for that action (its params schema)."
    )
    reminder: str | None = Field(
        None, description="A short message to send the user then, if they want to be told something."
    )
    no_single_action: bool = Field(
        False,
        description="Only true when no single action from list_actions fits; you will "
        "then be called back to work it out.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.settings.max_tier < 2:
        # A scheduled task fires into a run that needs trigger_action /
        # notify_user (ACTION tier); below tier 2 it would silently do nothing.
        return ToolResult.error(
            "feature_disabled",
            "Scheduling needs actions enabled (max_tier 2); a task would fire with nothing able to act.",
        )
    if not ctx.settings.clock_entity:
        return ToolResult.error("feature_disabled", "Time scheduling is off: no clock entity is configured.")
    if ctx.notes is None:
        return ToolResult.error("notes_unavailable", "The note store is unavailable.")
    now_local = await current_local_time(ctx)
    if now_local is None:
        return ToolResult.error(
            "clock_unavailable", f"Could not read the clock {ctx.settings.clock_entity!r}."
        )
    missing = await require_action_or_reminder(ctx, params)
    if missing is not None:
        return missing
    if params.action_entity_id:
        error = await validated_action(ctx, params.action_entity_id, params.action_params)
        if error is not None:
            return error
    try:
        fire_at = resolve_fire_at(now_local, params.at, params.in_minutes)
    except FireTimeError as exc:
        return ToolResult.error(exc.code, str(exc))

    expires_at = fire_at + timedelta(minutes=ctx.settings.time_note_grace_minutes)
    english, original = await english_instruction(ctx, params.instruction)
    reminder = None
    if params.reminder:
        reminder, _original = await english_instruction(ctx, params.reminder)
    scope = current_scope()
    note_id = ctx.notes.add_time_note(
        fire_at_local=fire_at,
        expires_at_local=expires_at,
        instruction=english,
        instruction_original=original,
        kind=task_kind(params.action_entity_id, reminder),
        language=scope.language,
        now=datetime.now(timezone.utc),
        source_thread_id=scope.thread_id,
        action_entity_id=params.action_entity_id,
        action_params=params.action_params,
        reminder=reminder,
    )
    if note_id is None:
        return ToolResult.error("notes_unavailable", "Could not save the task.")
    return ToolResult.ok({
        "id": note_id,
        "fire_at": format_clock(fire_at),
        "action": params.action_entity_id,
        "reminder": reminder,
    })


register(
    ToolDefinition(
        name="schedule_at_time",
        description=(
            "Make something happen at a clock time - use this instead of an automation. "
            "E.g. 'at 18:00 remind me to call mum' -> at='18:00', reminder='Call mum'; "
            "'in 20 minutes turn off the lights' -> in_minutes=20, action_entity_id=the "
            "action from list_actions. Exactly one of at / in_minutes. Do not act now."
        ),
        params_model=Params,
        tier=Tier.READ,  # agent-local state only; never writes to HA
        handler=handler,
    )
)
