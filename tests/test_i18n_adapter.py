"""LanguageAdapter: inbound/outbound flows, thread prior, fail-open."""

import pytest

from app.i18n.adapter import LanguageAdapter, NoopLanguageAdapter
from app.i18n.client import LangMTError
from app.i18n.glossary import Glossary


GLOSSARY_YAML = """
- id: living_room
  en: living room
  ru: [гостиная, гостиной]
"""


class FakeMT:
    """Records calls and replays queued responses (or raises queued errors)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def translate(self, text, src, tgt, allowed, prior=None):
        self.calls.append({"text": text, "src": src, "tgt": tgt, "allowed": allowed, "prior": prior})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class MT:
    """A translation result, as lang-mt would return it."""

    def __init__(self, text, src, confidence=0.99, translated=True):
        self.text = text
        self.src = src
        self.confidence = confidence
        self.translated = translated


@pytest.fixture()
def glossary(tmp_path):
    path = tmp_path / "glossary.yaml"
    path.write_text(GLOSSARY_YAML, encoding="utf-8")
    return Glossary.load(path)


def _adapter(client, glossary, **kwargs):
    return LanguageAdapter(
        client=client,
        glossary=glossary,
        languages=["en", "ru", "et"],
        default_language="en",
        min_confidence=0.5,
        **kwargs,
    )


async def test_noop_adapter_passes_text_through():
    adapter = NoopLanguageAdapter()
    inbound = await adapter.inbound("выключи свет", thread_id="t")
    assert inbound.english_text == "выключи свет"
    assert inbound.language == "en"
    assert inbound.translated is False
    assert await adapter.outbound("done", inbound) == "done"


async def test_inbound_protects_entities_and_restores_english(glossary):
    client = FakeMT([MT("turn off the light in the ⟦E1⟧", src="ru")])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("выключи свет в гостиной", thread_id="t")

    assert client.calls[0]["text"] == "выключи свет в ⟦E1⟧"
    assert client.calls[0]["src"] == "auto"
    assert client.calls[0]["tgt"] == "en"
    assert client.calls[0]["allowed"] == ["en", "ru", "et"]
    assert inbound.english_text == "turn off the light in the living room"
    assert inbound.language == "ru"
    assert inbound.translated is True
    assert inbound.original_text == "выключи свет в гостиной"
    assert inbound.glossary_hits == ["living_room"]
    assert inbound.source == "detector"


async def test_english_input_is_returned_unchanged(glossary):
    client = FakeMT([MT("turn off the light", src="en", translated=False)])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("turn off the light", thread_id="t")

    assert inbound.english_text == "turn off the light"
    assert inbound.language == "en"
    assert inbound.translated is False


async def test_thread_prior_is_sent_on_the_next_message(glossary):
    client = FakeMT([MT("the light", src="ru"), MT("the light", src="ru")])
    adapter = _adapter(client, glossary)

    await adapter.inbound("свет", thread_id="t")
    await adapter.inbound("свет", thread_id="t")

    assert client.calls[0]["prior"] is None
    assert client.calls[1]["prior"] == "ru"


async def test_priors_do_not_leak_between_threads(glossary):
    client = FakeMT([MT("the light", src="ru"), MT("the light", src="et")])
    adapter = _adapter(client, glossary)

    await adapter.inbound("свет", thread_id="a")
    await adapter.inbound("tuled", thread_id="b")

    assert client.calls[1]["prior"] is None


async def test_low_confidence_falls_back_to_the_thread_prior(glossary):
    client = FakeMT([
        MT("the light", src="ru", confidence=0.9),
        MT("?", src="et", confidence=0.2),
        MT("the light", src="ru", confidence=0.9),
    ])
    adapter = _adapter(client, glossary)

    await adapter.inbound("свет", thread_id="t")
    inbound = await adapter.inbound("свет", thread_id="t")

    assert inbound.language == "ru"
    assert inbound.source == "thread_prior"
    # The uncertain translation is redone with the prior as an explicit source.
    assert client.calls[2]["src"] == "ru"
    assert inbound.english_text == "the light"


async def test_low_confidence_without_a_prior_uses_the_default_language(glossary):
    client = FakeMT([MT("hmm", src="et", confidence=0.1)])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("hmm", thread_id="t")

    assert inbound.language == "en"
    assert inbound.source == "default"
    # Default is English, so the original text goes to the agent untranslated.
    assert inbound.english_text == "hmm"
    assert inbound.translated is False


async def test_inbound_fails_open_when_lang_mt_is_down(glossary):
    client = FakeMT([LangMTError("mt_timeout")])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("выключи свет", thread_id="t")

    assert inbound.english_text == "выключи свет"
    assert inbound.translated is False
    assert inbound.error == "mt_timeout"
    assert inbound.language == "en"


async def test_outbound_translates_the_reply_and_restores_native_names(glossary):
    client = FakeMT([
        MT("turned off the light in the ⟦E1⟧", src="ru"),
        MT("Выключил свет в ⟦E1⟧", src="en"),
    ])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("выключи свет в гостиной", thread_id="t")
    reply = await adapter.outbound("Turned off the light in the living room.", inbound)

    assert client.calls[1]["text"] == "Turned off the light in the ⟦E1⟧."
    assert client.calls[1]["src"] == "en"
    assert client.calls[1]["tgt"] == "ru"
    assert reply == "Выключил свет в гостиная"


async def test_outbound_is_a_passthrough_for_english(glossary):
    client = FakeMT([MT("hello", src="en", translated=False)])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("hello", thread_id="t")
    assert await adapter.outbound("Hi there.", inbound) == "Hi there."
    assert len(client.calls) == 1


async def test_outbound_returns_english_when_translation_fails(glossary):
    client = FakeMT([MT("the light", src="ru"), LangMTError("mt_placeholder_lost")])
    adapter = _adapter(client, glossary)

    inbound = await adapter.inbound("свет", thread_id="t")
    reply = await adapter.outbound("The light is on.", inbound)

    assert reply == "The light is on."
    assert inbound.error == "mt_placeholder_lost"
