"""Tool envelope -> short English text. Pure functions; no I/O."""

from __future__ import annotations

ALIAS_PREFIX = "AI Action:"  # house convention for ai_* script aliases

_ERROR_TEXT = {
    "ha_unreachable": "Couldn't reach Home Assistant.",
    "ha_timeout": "Home Assistant did not answer in time.",
}


def display_action_name(name: str, entity_id: str = "") -> str:
    if name:
        if name.lower().startswith(ALIAS_PREFIX.lower()):
            return name[len(ALIAS_PREFIX):].strip()
        return name
    local = entity_id.split(".", 1)[-1]
    if local.startswith("ai_"):
        local = local[len("ai_"):]
    return local.replace("_", " ")


def _sort_key(row: dict) -> str:
    return display_action_name(row.get("name", ""), row.get("entity_id", "")).lower()


def sorted_action_rows(rows: list[dict]) -> list[dict]:
    """The one ordering /actions numbers and /run <n> resolves against."""
    result = list(rows)
    result.sort(key=_sort_key)
    return result


def _row_label(row: dict) -> str:
    return display_action_name(row.get("name", ""), row.get("entity_id", ""))


def format_actions(rows: list[dict], total: int) -> str:
    if not rows:
        return "No AI actions are available."
    lines = ["Actions:"]
    number = 1
    for row in rows:
        lines.append(f"{number}. {_row_label(row)}")
        number += 1
    if total > len(rows):
        lines.append(f"(showing {len(rows)} of {total})")
    lines.append("Run one with /run <number or name>.")
    return "\n".join(lines)


def format_did_you_mean(candidates: list[dict], rows: list[dict]) -> str:
    lines = ["Did you mean:"]
    for candidate in candidates:
        number = rows.index(candidate) + 1
        lines.append(f"{number}. {_row_label(candidate)}")
    lines.append("Reply /run <number>.")
    return "\n".join(lines)


def format_error(envelope: dict) -> str:
    error = envelope.get("error") or {}
    code = error.get("code") or ""
    if code in _ERROR_TEXT:
        return _ERROR_TEXT[code]
    return error.get("message") or "Something went wrong."


def format_trigger(envelope: dict, name: str, params: dict) -> str:
    # Same wording as the fast path's templated confirmation.
    if envelope.get("status") != "ok":
        return format_error(envelope)
    if params:
        parts = []
        for key, value in params.items():
            parts.append(f"{key}: {value}")
        return f"Done — triggered {name} ({', '.join(parts)})."
    return f"Done — triggered {name}."


def format_notes(envelope: dict) -> str:
    if envelope.get("status") != "ok":
        return format_error(envelope)
    rows = (envelope.get("data") or {}).get("rows") or []
    if not rows:
        return "No scheduled tasks."
    lines = ["Scheduled tasks:"]
    for row in rows:
        lines.append(f"#{row['id']} — {row.get('when', '')}: {row.get('instruction', '')}")
    lines.append("Cancel one with /cancel <id>.")
    return "\n".join(lines)
