from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.run_scope import current_scope
from app.events.clock import FireTimeError, current_local_time, format_clock, resolve_fire_at
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.notes import english_instruction
from app.tools.registry import register


class Params(BaseModel):
    at: str | None = Field(
        None, description="Local time 'HH:MM' (next occurrence) or 'HH:MM DD-MM-YYYY'."
    )
    in_minutes: int | None = Field(
        None, description="Minutes from now, for 'in 20 minutes'. Use instead of 'at'."
    )
    instruction: str = Field(description="What to do then, as one self-contained sentence.")
    kind: Literal["action", "reminder"] = Field(
        description="'reminder' = tell the user something; 'action' = make the house do something."
    )


async def handler(params: Params, ctx) -> ToolResult:
    if not ctx.settings.clock_entity:
        return ToolResult.error("feature_disabled", "Time scheduling is off: no clock entity is configured.")
    if ctx.notes is None:
        return ToolResult.error("notes_unavailable", "The note store is unavailable.")
    now_local = await current_local_time(ctx)
    if now_local is None:
        return ToolResult.error(
            "clock_unavailable", f"Could not read the clock {ctx.settings.clock_entity!r}."
        )
    try:
        fire_at = resolve_fire_at(now_local, params.at, params.in_minutes)
    except FireTimeError as exc:
        return ToolResult.error(exc.code, str(exc))

    expires_at = fire_at + timedelta(minutes=ctx.settings.time_note_grace_minutes)
    english, original = await english_instruction(ctx, params.instruction)
    scope = current_scope()
    note_id = ctx.notes.add_time_note(
        fire_at_local=fire_at,
        expires_at_local=expires_at,
        instruction=english,
        instruction_original=original,
        kind=params.kind,
        language=scope.language,
        now=datetime.now(timezone.utc),
        source_thread_id=scope.thread_id,
    )
    if note_id is None:
        return ToolResult.error("notes_unavailable", "Could not save the task.")
    return ToolResult.ok({"id": note_id, "fire_at": format_clock(fire_at)})


register(
    ToolDefinition(
        name="schedule_at_time",
        description=(
            "Make something happen at a clock time - use this instead of an automation. "
            "E.g. 'remind me at 18:00 to call mum' -> at='18:00', kind='reminder'; 'in 20 "
            "minutes turn off the lights' -> in_minutes=20, kind='action'. Exactly one of "
            "at / in_minutes. At that time you are called back and carry out `instruction` "
            "with your normal tools. Do not act now."
        ),
        params_model=Params,
        tier=Tier.READ,  # agent-local state only; never writes to HA
        handler=handler,
    )
)
