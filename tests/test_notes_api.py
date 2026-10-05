from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.memory.store import NoteStore


def _settings():
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
    )


def _store_with_notes():
    store = NoteStore("")
    now = datetime.now(timezone.utc)
    ids = []
    for text in ("Stop the vacuum.", "Old note."):
        ids.append(store.add_state_note(
            entity_id="vacuum.roborock_qrevo_s", to_state="cleaning", instruction=text,
            instruction_original=None, kind="action", language="en", tags=[],
            expires_at=now + timedelta(hours=24), now=now, source_thread_id="t1",
        ))
    store.mark_fired([ids[1]], now)
    return store, ids[0], ids[1]


def _ids(body):
    ids = []
    for note in body["notes"]:
        ids.append(note["id"])
    return ids


def test_list_pending_notes():
    store, pending, _fired = _store_with_notes()
    with TestClient(create_app(_settings())) as client:
        client.app.state.notes = store
        body = client.get("/api/notes").json()
    assert _ids(body) == [pending]
    assert body["notes"][0]["instruction"] == "Stop the vacuum."


def test_list_all_notes_includes_history():
    store, pending, fired = _store_with_notes()
    with TestClient(create_app(_settings())) as client:
        client.app.state.notes = store
        body = client.get("/api/notes?status=all").json()
    assert _ids(body) == [fired, pending]


def test_delete_cancels_pending_note():
    store, pending, _fired = _store_with_notes()
    with TestClient(create_app(_settings())) as client:
        client.app.state.notes = store
        assert client.delete(f"/api/notes/{pending}").json() == {"ok": True}
        assert client.get("/api/notes").json()["notes"] == []
        assert client.delete(f"/api/notes/{pending}").status_code == 404


def test_delete_fired_note_is_404():
    store, _pending, fired = _store_with_notes()
    with TestClient(create_app(_settings())) as client:
        client.app.state.notes = store
        assert client.delete(f"/api/notes/{fired}").status_code == 404


def test_notes_unavailable_is_503():
    with TestClient(create_app(_settings())) as client:
        client.app.state.notes = None
        assert client.get("/api/notes").status_code == 503
        assert client.delete("/api/notes/1").status_code == 503
