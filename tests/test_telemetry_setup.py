"""Tests for app/telemetry/setup.py — TracerProvider lifecycle."""

from app.config import Settings
from app.telemetry.setup import init_telemetry, shutdown_telemetry


def test_disabled_returns_none():
    s = Settings(_env_file=None, telemetry_enabled=False)
    assert init_telemetry(s) is None


def test_empty_db_path_returns_none():
    s = Settings(_env_file=None, telemetry_enabled=True, telemetry_db_path="")
    assert init_telemetry(s) is None


def test_unwritable_db_disables_but_does_not_raise():
    s = Settings(_env_file=None, telemetry_enabled=True, telemetry_db_path="/does/not/exist/telemetry.sqlite")
    provider = init_telemetry(s)   # logs once, returns None
    assert provider is None


def test_enabled_returns_provider(tmp_path):
    s = Settings(_env_file=None, telemetry_enabled=True, telemetry_db_path=str(tmp_path / "t.sqlite"))
    provider = init_telemetry(s)
    assert provider is not None
    shutdown_telemetry(provider)
