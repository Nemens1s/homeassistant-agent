from app.degraded.formatters import (
    display_action_name,
    format_actions,
    format_did_you_mean,
    format_error,
    format_notes,
    format_trigger,
    sorted_action_rows,
)

ROWS = [
    {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum"},
    {"entity_id": "automation.ai_goodnight", "name": "Goodnight"},
    {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Living Room Lights On"},
]


def test_display_action_name_strips_prefix_and_falls_back_to_entity_id():
    assert display_action_name("AI Action: Stop Vacuum") == "Stop Vacuum"
    assert display_action_name("", "script.ai_action_stop_vacuum") == "action stop vacuum"


def test_sorted_action_rows_by_display_name():
    names = []
    for row in sorted_action_rows(ROWS):
        names.append(display_action_name(row["name"], row["entity_id"]))
    assert names == ["Goodnight", "Living Room Lights On", "Stop Vacuum"]


def test_format_actions_numbers_rows():
    text = format_actions(sorted_action_rows(ROWS), total=3)
    assert "1. Goodnight" in text
    assert "3. Stop Vacuum" in text
    assert "/run" in text


def test_format_actions_empty_and_truncated():
    assert format_actions([], total=0) == "No AI actions are available."
    assert "showing 1 of 5" in format_actions(sorted_action_rows(ROWS[:1]), total=5)


def test_format_did_you_mean_uses_actions_numbering():
    rows = sorted_action_rows(ROWS)
    text = format_did_you_mean([rows[2], rows[1]], rows)
    assert "3. Stop Vacuum" in text
    assert "2. Living Room Lights On" in text
    assert "/run <number>" in text


def test_format_trigger_ok_and_error():
    ok = {"status": "ok", "data": {"triggered": True}}
    assert format_trigger(ok, "Lights On", {}) == "Done — triggered Lights On."
    assert format_trigger(ok, "Dim", {"level": 30}) == "Done — triggered Dim (level: 30)."
    refused = {"status": "error", "error": {"code": "ai_disabled", "message": "AI-triggered actions are turned off at the home level."}}
    assert format_trigger(refused, "Lights On", {}).startswith("AI-triggered actions are turned off")


def test_format_error_maps_unreachable():
    env = {"status": "error", "error": {"code": "ha_unreachable", "message": "Could not reach Home Assistant: boom."}}
    assert format_error(env) == "Couldn't reach Home Assistant."


def test_format_notes():
    env = {"status": "ok", "data": {"rows": [
        {"id": 12, "kind": "action", "instruction": "Stop the vacuum.", "when": "18:00 16-10-2026"},
    ], "total": 1}}
    assert format_notes(env) == "Scheduled tasks:\n#12 — 18:00 16-10-2026: Stop the vacuum.\nCancel one with /cancel <id>."
    assert format_notes({"status": "ok", "data": {"rows": [], "total": 0}}) == "No scheduled tasks."
