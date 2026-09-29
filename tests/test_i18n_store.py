"""Native-text overlay for /api/history."""

import sqlite3

import pytest

from app.i18n.store import LangOverlay, apply_overlay


@pytest.fixture()
def overlay(tmp_path):
    store = LangOverlay(str(tmp_path / "checkpoints.sqlite"))
    yield store
    store.close()


def test_table_is_created_in_the_given_database(tmp_path):
    path = tmp_path / "checkpoints.sqlite"
    LangOverlay(str(path)).close()
    conn = sqlite3.connect(path)
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    assert "lang_overlay" in names


def test_record_then_read_back_in_order(overlay):
    overlay.record("t1", language="ru", original_text="свет", english_text="light",
                   reply_en="It is on.", reply_native="Он включён.")
    overlay.record("t1", language="ru", original_text="а теперь?", english_text="and now?",
                   reply_en="Off.", reply_native="Выключен.")

    rows = overlay.rows_for("t1")
    assert [r["turn_index"] for r in rows] == [0, 1]
    assert rows[0]["original_text"] == "свет"
    assert rows[1]["reply_native"] == "Выключен."


def test_rows_are_scoped_to_a_thread(overlay):
    overlay.record("t1", language="ru", original_text="свет", english_text="light",
                   reply_en="On.", reply_native="Включён.")
    assert overlay.rows_for("t2") == []


def test_old_rows_are_pruned_per_thread(tmp_path):
    store = LangOverlay(str(tmp_path / "db.sqlite"), max_rows_per_thread=3)
    for i in range(5):
        store.record("t1", language="ru", original_text=f"o{i}", english_text=f"e{i}",
                     reply_en=f"r{i}", reply_native=f"n{i}")
    rows = store.rows_for("t1")
    store.close()
    assert [r["english_text"] for r in rows] == ["e2", "e3", "e4"]


def test_a_broken_database_does_not_raise(tmp_path):
    path = tmp_path / "not-a-db.sqlite"
    path.write_text("this is not sqlite", encoding="utf-8")
    store = LangOverlay(str(path))
    # Never raises into a request: recording and reading just do nothing.
    store.record("t1", language="ru", original_text="o", english_text="e",
                 reply_en="r", reply_native="n")
    assert store.rows_for("t1") == []
    store.close()


# --- substitution -----------------------------------------------------------
def _row(english_text, original_text, reply_en, reply_native, turn_index=0):
    return {
        "turn_index": turn_index,
        "language": "ru",
        "original_text": original_text,
        "english_text": english_text,
        "reply_en": reply_en,
        "reply_native": reply_native,
    }


def test_apply_overlay_swaps_both_sides_of_a_turn():
    turns = [
        {"role": "user", "text": "turn off the light"},
        {"role": "bot", "text": "Turned it off."},
    ]
    rows = [_row("turn off the light", "выключи свет", "Turned it off.", "Выключил.")]
    assert apply_overlay(turns, rows) == [
        {"role": "user", "text": "выключи свет"},
        {"role": "bot", "text": "Выключил."},
    ]


def test_apply_overlay_leaves_english_turns_alone():
    turns = [
        {"role": "user", "text": "hello"},
        {"role": "bot", "text": "Hi."},
        {"role": "user", "text": "turn off the light"},
        {"role": "bot", "text": "Turned it off."},
    ]
    rows = [_row("turn off the light", "выключи свет", "Turned it off.", "Выключил.")]
    assert apply_overlay(turns, rows) == [
        {"role": "user", "text": "hello"},
        {"role": "bot", "text": "Hi."},
        {"role": "user", "text": "выключи свет"},
        {"role": "bot", "text": "Выключил."},
    ]


def test_apply_overlay_keeps_english_when_the_reply_was_not_translated():
    turns = [
        {"role": "user", "text": "turn off the light"},
        {"role": "bot", "text": "Turned it off."},
    ]
    rows = [_row("turn off the light", "выключи свет", "Turned it off.", None)]
    assert apply_overlay(turns, rows) == [
        {"role": "user", "text": "выключи свет"},
        {"role": "bot", "text": "Turned it off."},
    ]


def test_apply_overlay_skips_rows_whose_turns_were_pruned_by_the_history_cap():
    turns = [
        {"role": "user", "text": "and now?"},
        {"role": "bot", "text": "Off."},
    ]
    rows = [
        _row("turn off the light", "выключи свет", "Turned it off.", "Выключил.", 0),
        _row("and now?", "а теперь?", "Off.", "Выключен.", 1),
    ]
    assert apply_overlay(turns, rows) == [
        {"role": "user", "text": "а теперь?"},
        {"role": "bot", "text": "Выключен."},
    ]


def test_apply_overlay_without_rows_is_the_identity():
    turns = [{"role": "user", "text": "hello"}, {"role": "bot", "text": "Hi."}]
    assert apply_overlay(turns, []) == turns


def test_repeated_identical_questions_consume_rows_in_order():
    turns = [
        {"role": "user", "text": "the light?"},
        {"role": "bot", "text": "On."},
        {"role": "user", "text": "the light?"},
        {"role": "bot", "text": "Off."},
    ]
    rows = [
        _row("the light?", "свет?", "On.", "Включён.", 0),
        _row("the light?", "свет?", "Off.", "Выключен.", 1),
    ]
    assert apply_overlay(turns, rows) == [
        {"role": "user", "text": "свет?"},
        {"role": "bot", "text": "Включён."},
        {"role": "user", "text": "свет?"},
        {"role": "bot", "text": "Выключен."},
    ]
