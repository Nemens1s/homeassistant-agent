"""Tests for telemetry Settings defaults."""

from app.config import Settings


def test_telemetry_defaults():
    # _env_file=None so a developer's local .env (e.g. TELEMETRY_DB_PATH) never
    # leaks into the default assertions — matches the suite's house style.
    s = Settings(_env_file=None)
    assert s.telemetry_enabled is True
    assert s.telemetry_db_path == "/data/telemetry.sqlite"
