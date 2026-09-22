from app.telemetry.store import open_store
from app.telemetry.snapshots import menu_hash, register_menu, content_hash
from app.needle.menu import Menu, MenuItem


def test_menu_hash_changes_on_description_but_signature_would_not():
    m1 = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="sig")
    m2 = Menu(items=(MenuItem("automation.ai_action_night", "Night", "sleep well"),), signature="sig")
    assert menu_hash(m1) != menu_hash(m2)          # description is part of the hash
    assert m1.signature == m2.signature            # signature is entity-id only


def test_hash_is_16_hex():
    h = content_hash({"a": 1})
    assert len(h) == 16 and all(ch in "0123456789abcdef" for ch in h)


def test_register_menu_writes_once(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    m = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="sig")
    h1 = register_menu(conn, m)
    h2 = register_menu(conn, m)
    assert h1 == h2
    assert conn.execute("SELECT COUNT(*) FROM menu_snapshots WHERE hash=?", (h1,)).fetchone()[0] == 1
