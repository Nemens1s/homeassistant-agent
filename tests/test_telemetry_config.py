"""Tests for telemetry Settings defaults and the OTLP LAN guard."""

import pytest

from app.config import Settings
from app.main import _assert_otlp_is_lan


def test_telemetry_defaults():
    # _env_file=None so a developer's local .env (e.g. TELEMETRY_DB_PATH) never
    # leaks into the default assertions — matches the suite's house style.
    s = Settings(_env_file=None)
    assert s.telemetry_enabled is True
    assert s.telemetry_db_path == "/data/telemetry.sqlite"
    assert s.telemetry_retention_days == 0
    assert s.otlp_endpoint == ""


# ---------------------------------------------------------------------------
# OTLP LAN guard
# ---------------------------------------------------------------------------

# Private / loopback addresses that must be accepted.
_PRIVATE_URLS = [
    "http://127.0.0.1:4318",
    "http://localhost:4318",
    "http://192.168.1.100:4318",
    "http://10.0.0.1:4318",
    "http://172.16.0.1:4318",
]

# Public addresses that must be refused.
_PUBLIC_URLS = [
    "http://8.8.8.8:4318",
    "http://1.1.1.1:4318",
]


@pytest.mark.parametrize("url", _PRIVATE_URLS)
def test_otlp_lan_guard_accepts_private(url):
    """Private and loopback OTLP endpoints must not raise."""
    _assert_otlp_is_lan(url)  # should not raise


@pytest.mark.parametrize("url", _PUBLIC_URLS)
def test_otlp_lan_guard_rejects_public(url):
    """Public OTLP endpoints must raise RuntimeError."""
    with pytest.raises(RuntimeError, match="public address"):
        _assert_otlp_is_lan(url)


def test_otlp_lan_guard_rejects_unparseable_url():
    """An endpoint with no parseable hostname must raise RuntimeError."""
    with pytest.raises(RuntimeError, match="cannot parse hostname"):
        _assert_otlp_is_lan("not-a-url")
