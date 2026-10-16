"""Slash commands. Each takes the text after the command word and a
ToolRunner, and returns English text. run_action_row / list_action_rows are
shared with the natural-language fallback in router.py."""

from __future__ import annotations

from app.degraded.formatters import (
    display_action_name,
    format_actions,
    format_did_you_mean,
    format_error,
    format_notes,
    format_trigger,
    sorted_action_rows,
)
from app.degraded.matcher import match_action
from app.health import DOWN, UP

NOT_AVAILABLE = "Not available in this home."

HELP_TEXT = "\n".join([
    "Commands:",
    "/actions — list the actions I can run",
    "/run <number or name> [key=value …] — run one",
    "/notes — list scheduled tasks",
    "/cancel <id> — cancel a scheduled task",
    "/help — this list",
])


def llm_status_line(llm_health) -> str:
    if llm_health is None:
        return "LLM: status unknown"
    if llm_health.state == UP:
        return "LLM: up"
    if llm_health.state == DOWN:
        return f"LLM: offline since {llm_health.since_hhmm()}"
    return "LLM: status unknown"


def _coerce(value: str):
    digits = value
    if digits.startswith("-"):
        digits = digits[1:]
    if digits.isdigit():
        return int(value)
    return value


def parse_run_args(arg: str) -> tuple[str, dict]:
    """'Dim level=30' -> ('Dim', {'level': 30}). Tokens with '=' are params;
    the rest, joined, is the action number or name."""
    name_parts = []
    params = {}
    for token in arg.split():
        if "=" in token and not token.startswith("="):
            key, value = token.split("=", 1)
            params[key] = _coerce(value)
        else:
            name_parts.append(token)
    return " ".join(name_parts), params


async def list_action_rows(runner) -> list[dict] | str:
    envelope = await runner.run("list_actions", {})
    if envelope.get("status") != "ok":
        return format_error(envelope)
    rows = (envelope.get("data") or {}).get("rows") or []
    return sorted_action_rows(rows)


async def run_action_row(row: dict, params: dict, runner) -> str:
    name = display_action_name(row.get("name", ""), row.get("entity_id", ""))
    required = (row.get("params") or {}).get("required") or []
    missing = []
    for key in required:
        if key not in params:
            missing.append(key)
    if missing:
        example_parts = []
        for key in missing:
            example_parts.append(f"{key}=…")
        return f"{name} needs: {', '.join(missing)}. Example: /run {name} {' '.join(example_parts)}"
    envelope = await runner.run(
        "trigger_action", {"entity_id": row["entity_id"], "params": params},
    )
    return format_trigger(envelope, name, params)


async def cmd_help(arg: str, runner, llm_health) -> str:
    return HELP_TEXT + "\n" + llm_status_line(llm_health)


async def cmd_actions(arg: str, runner) -> str:
    if not runner.has("list_actions"):
        return NOT_AVAILABLE
    envelope = await runner.run("list_actions", {})
    if envelope.get("status") != "ok":
        return format_error(envelope)
    data = envelope.get("data") or {}
    rows = sorted_action_rows(data.get("rows") or [])
    return format_actions(rows, total=data.get("total", len(rows)))


async def cmd_run(arg: str, runner) -> str:
    if not runner.has("list_actions") or not runner.has("trigger_action"):
        return NOT_AVAILABLE
    name, params = parse_run_args(arg)
    if not name:
        return "Usage: /run <number or name> [key=value …]. See /actions."
    rows = await list_action_rows(runner)
    if isinstance(rows, str):
        return rows
    if name.isdigit():
        number = int(name)
        if number < 1 or number > len(rows):
            return f"No action number {number}. See /actions."
        return await run_action_row(rows[number - 1], params, runner)
    match = match_action(name, rows)
    if match.winner is not None:
        return await run_action_row(match.winner, params, runner)
    if match.candidates:
        return format_did_you_mean(match.candidates, rows)
    return f"No action matches “{name}”. See /actions."


async def cmd_notes(arg: str, runner) -> str:
    if not runner.has("list_scheduled"):
        return NOT_AVAILABLE
    return format_notes(await runner.run("list_scheduled", {}))


async def cmd_cancel(arg: str, runner) -> str:
    if not runner.has("cancel_scheduled"):
        return NOT_AVAILABLE
    text = arg.strip()
    if not text.isdigit():
        return "Usage: /cancel <id>. See /notes."
    envelope = await runner.run("cancel_scheduled", {"id": int(text)})
    if envelope.get("status") != "ok":
        return format_error(envelope)
    return f"Cancelled task #{text}."
