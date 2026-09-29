"""Language-layer telemetry: gosling.lang.* on the root span, child spans
for the two MT hops."""

import json

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.i18n.adapter import LanguageAdapter
from app.i18n.client import LangMTError
from app.i18n.glossary import Glossary
from app.main import create_app
from app.telemetry import conventions as C


class FakeMT:
    def __init__(self, responses):
        self._responses = list(responses)

    async def translate(self, text, src, tgt, allowed, prior=None):
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def aclose(self):
        return None


class MT:
    def __init__(self, text, src, confidence=0.99, translated=True):
        self.text = text
        self.src = src
        self.confidence = confidence
        self.translated = translated


def _tracer(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test")


def _adapter(mt, tmp_path, tracer):
    glossary_file = tmp_path / "glossary.yaml"
    glossary_file.write_text(
        "- id: living_room\n  en: living room\n  ru: [гостиная, гостиной]\n", encoding="utf-8"
    )
    return LanguageAdapter(
        client=mt,
        glossary=Glossary.load(glossary_file),
        languages=["en", "ru", "et"],
        default_language="en",
        min_confidence=0.5,
        tracer=tracer,
    )


async def test_adapter_emits_child_spans_for_both_hops(tmp_path):
    exporter = InMemorySpanExporter()
    mt = FakeMT([MT("the ⟦E1⟧ light", src="ru"), MT("свет в ⟦E1⟧", src="en")])
    adapter = _adapter(mt, tmp_path, _tracer(exporter))

    inbound = await adapter.inbound("свет в гостиной", thread_id="t")
    await adapter.outbound("the living room light", inbound)

    names = [s.name for s in exporter.get_finished_spans()]
    assert names == [C.SPAN_LANG_INBOUND, C.SPAN_LANG_OUTBOUND]


async def test_inbound_span_records_the_detection(tmp_path):
    exporter = InMemorySpanExporter()
    adapter = _adapter(FakeMT([MT("the light", src="ru", confidence=0.94)]), tmp_path, _tracer(exporter))

    await adapter.inbound("свет", thread_id="t")

    span = exporter.get_finished_spans()[0]
    assert span.attributes[C.GOSLING_LANG_DETECTED] == "ru"
    assert span.attributes[C.GOSLING_LANG_CONFIDENCE] == 0.94
    assert span.attributes[C.GOSLING_LANG_SOURCE] == "detector"


def test_root_span_carries_the_language_attributes(tmp_path, reset_otel_provider):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)

    mt = FakeMT([
        MT("turn off the light in the ⟦E1⟧", src="ru", confidence=0.94),
        MT("Выключил свет в ⟦E1⟧", src="en"),
    ])

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [AIMessage(content="Turned off the light in the living room.")]}

    settings = Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        client.app.state.language = _adapter(mt, tmp_path, provider.get_tracer("test"))
        client.post("/api/chat", json={"message": "выключи свет в гостиной"})

    root = [s for s in exporter.get_finished_spans() if s.name == C.SPAN_INVOKE_AGENT][0]
    assert root.attributes[C.GOSLING_LANG_ENABLED] is True
    assert root.attributes[C.GOSLING_LANG_DETECTED] == "ru"
    assert root.attributes[C.GOSLING_LANG_CONFIDENCE] == 0.94
    assert root.attributes[C.GOSLING_LANG_SOURCE] == "detector"
    assert root.attributes[C.GOSLING_LANG_ORIGINAL_TEXT] == "выключи свет в гостиной"
    assert root.attributes[C.GOSLING_LANG_REPLY_NATIVE] == "Выключил свет в гостиная"
    assert json.loads(root.attributes[C.GOSLING_LANG_GLOSSARY_HITS]) == ["living_room"]
    # The English text the agent saw stays in the existing attributes.
    assert root.attributes[C.GOSLING_INPUT_TEXT] == "turn off the light in the living room"
    assert root.attributes[C.GOSLING_OUTPUT_TEXT] == "Turned off the light in the living room."


def test_root_span_records_a_lang_mt_failure(tmp_path, reset_otel_provider):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [AIMessage(content="ok")]}

    settings = Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        client.app.state.language = _adapter(
            FakeMT([LangMTError("mt_timeout")]), tmp_path, provider.get_tracer("test")
        )
        resp = client.post("/api/chat", json={"message": "выключи свет"})

    assert resp.json()["reply"] == "ok"
    root = [s for s in exporter.get_finished_spans() if s.name == C.SPAN_INVOKE_AGENT][0]
    assert root.attributes[C.GOSLING_LANG_ERROR] == "mt_timeout"


def test_disabled_layer_marks_the_root_span_and_nothing_else(reset_otel_provider):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [AIMessage(content="ok")]}

    settings = Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        client.post("/api/chat", json={"message": "hello"})

    root = [s for s in exporter.get_finished_spans() if s.name == C.SPAN_INVOKE_AGENT][0]
    assert root.attributes[C.GOSLING_LANG_ENABLED] is False
    assert C.GOSLING_LANG_ORIGINAL_TEXT not in root.attributes
