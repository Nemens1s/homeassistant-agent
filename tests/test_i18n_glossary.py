"""Glossary: entity protection via placeholders."""

import pytest

from app.i18n.glossary import Glossary


GLOSSARY_YAML = """
- id: concorde_lights
  en: Concorde
  ru: [Конкорд, Конкорда, Конкорду]
  et: [Concorde, Concorde'i]
- id: living_room
  en: living room
  ru: [гостиная, гостиной, гостиную]
  et: [elutuba, elutoas, elutuppa]
"""


@pytest.fixture()
def glossary(tmp_path):
    path = tmp_path / "glossary.yaml"
    path.write_text(GLOSSARY_YAML, encoding="utf-8")
    return Glossary.load(path)


def test_missing_file_gives_empty_glossary(tmp_path):
    g = Glossary.load(tmp_path / "nope.yaml")
    protected = g.protect("turn on the lights")
    assert protected.text == "turn on the lights"
    assert protected.hits == []


def test_protect_replaces_a_russian_inflected_form(glossary):
    protected = glossary.protect("включи Конкорда в гостиной")
    assert protected.text == "включи [E1] в [E2]"
    assert protected.hits == ["concorde_lights", "living_room"]


def test_restore_puts_english_canonical_names_back(glossary):
    protected = glossary.protect("включи Конкорда в гостиной")
    english = "turn on [E1] in the [E2]"
    assert glossary.restore(english, protected, "en") == "turn on Concorde in the living room"


def test_restore_uses_the_first_native_form(glossary):
    protected = glossary.protect("Turned on Concorde in the living room", languages=["en"])
    assert protected.text == "Turned on [E1] in the [E2]"
    native = "Включил [E1] в [E2]"
    assert glossary.restore(native, protected, "ru") == "Включил Конкорд в гостиная"


def test_matching_is_case_insensitive(glossary):
    protected = glossary.protect("turn on CONCORDE")
    assert protected.text == "turn on [E1]"


def test_longest_match_wins(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text(
        "- id: room\n  en: room\n- id: living_room\n  en: living room\n",
        encoding="utf-8",
    )
    g = Glossary.load(path)
    protected = g.protect("the living room is warm")
    assert protected.hits == ["living_room"]
    assert g.restore(protected.text, protected, "en") == "the living room is warm"


def test_protect_limited_to_one_language_ignores_other_forms(glossary):
    protected = glossary.protect("включи Конкорда", languages=["en"])
    assert protected.text == "включи Конкорда"
    assert protected.hits == []


def test_restore_falls_back_to_english_when_language_has_no_form(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text("- id: vacuum\n  en: vacuum\n", encoding="utf-8")
    g = Glossary.load(path)
    protected = g.protect("start the vacuum", languages=["en"])
    assert g.restore(protected.text, protected, "ru") == "start the vacuum"


def test_placeholder_format_is_bracketed():
    # NLLB's sentencepiece vocabulary has no ⟦ or ⟧: they tokenize to <unk>
    # and the placeholder cannot survive translation.
    from app.i18n.glossary import PLACEHOLDER_RE, placeholder

    assert placeholder(1) == "[E1]"
    assert PLACEHOLDER_RE.findall("turn on [E1] in [E2]") == ["[E1]", "[E2]"]
