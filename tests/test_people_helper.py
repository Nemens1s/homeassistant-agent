from app.config import Settings
from app.tools.helpers.people import alias_legend, resolve_name


def _settings(name_map=None):
    return Settings(_env_file=None, person_name_map=name_map or [])


def test_alias_legend_groups_aliases_by_canonical():
    s = _settings([
        {"ha_name": "megakrasotka2002", "name": "Sofija"},
        {"ha_name": "Sonja", "name": "Sofija"},
        {"ha_name": "Sonya", "name": "Sofija"},
    ])
    assert alias_legend(s) == {"Sofija": ["Sonja", "Sonya", "megakrasotka2002"]}


def test_alias_legend_empty_when_no_map():
    assert alias_legend(_settings()) == {}


def test_alias_legend_omits_canonical_with_single_name():
    s = _settings([{"ha_name": "ilniko", "name": "Ilja"}])
    assert alias_legend(s) == {}


def test_resolve_name_returns_canonical_and_other_aliases():
    s = _settings([
        {"ha_name": "megakrasotka2002", "name": "Sofija"},
        {"ha_name": "Sonja", "name": "Sofija"},
        {"ha_name": "Sonya", "name": "Sofija"},
    ])
    canonical, aliases = resolve_name("megakrasotka2002", s)
    assert canonical == "Sofija"
    assert set(aliases) == {"Sonja", "Sonya"}


def test_resolve_name_unknown_passes_through_with_no_aliases():
    s = _settings([{"ha_name": "ilniko", "name": "Ilja"}])
    canonical, aliases = resolve_name("Guest", s)
    assert canonical == "Guest"
    assert aliases == []
