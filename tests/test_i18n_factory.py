"""Config flag and adapter construction."""

from app.config import Settings
from app.i18n import build_language_adapter
from app.i18n.adapter import LanguageAdapter, NoopLanguageAdapter


def test_language_layer_defaults_are_off_and_safe():
    s = Settings(_env_file=None)
    assert s.language_layer_enabled is False
    assert s.languages == ["en", "ru", "et"]
    assert s.default_language == "en"
    assert s.min_confidence == 0.5
    assert s.lang_mt_timeout_s == 3.0
    assert s.glossary_path == "/config/gosling/glossary.yaml"


def test_disabled_settings_build_a_noop_adapter():
    adapter = build_language_adapter(Settings(_env_file=None))
    assert isinstance(adapter, NoopLanguageAdapter)
    assert adapter.enabled is False


def test_enabled_settings_build_a_real_adapter(tmp_path):
    glossary = tmp_path / "glossary.yaml"
    glossary.write_text("- id: living_room\n  en: living room\n", encoding="utf-8")
    s = Settings(
        _env_file=None,
        language_layer_enabled=True,
        lang_mt_url="http://mt.test:10100",
        glossary_path=str(glossary),
    )
    adapter = build_language_adapter(s)
    assert isinstance(adapter, LanguageAdapter)
    assert adapter.enabled is True


def test_a_missing_glossary_does_not_block_startup(tmp_path):
    s = Settings(
        _env_file=None,
        language_layer_enabled=True,
        glossary_path=str(tmp_path / "absent.yaml"),
    )
    adapter = build_language_adapter(s)
    assert isinstance(adapter, LanguageAdapter)
