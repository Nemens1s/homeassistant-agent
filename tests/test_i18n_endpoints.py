"""The adapter wired into /api/chat and /api/chat/stream."""

import json

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import Settings
from app.i18n.adapter import LanguageAdapter, NoopLanguageAdapter
from app.i18n.glossary import Glossary
from app.main import create_app


class FakeMT:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def translate(self, text, src, tgt, allowed, prior=None):
        self.calls.append({"text": text, "src": src, "tgt": tgt})
        return self._responses.pop(0)

    async def aclose(self):
        return None


class MT:
    def __init__(self, text, src, confidence=0.99, translated=True):
        self.text = text
        self.src = src
        self.confidence = confidence
        self.translated = translated


class FakeAgent:
    """Records what the agent core actually received."""

    def __init__(self, reply="Turned off the light."):
        self.reply = reply
        self.seen = []

    async def ainvoke(self, payload, config=None):
        self.seen.append(payload["messages"][-1]["content"])
        return {"messages": [AIMessage(content=self.reply)]}

    async def astream(self, payload, config=None, stream_mode=None):
        self.seen.append(payload["messages"][-1]["content"])
        yield AIMessage(content=self.reply), {}


def _settings(**kwargs):
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=False,
        **kwargs,
    )


def _adapter(mt, tmp_path):
    glossary_file = tmp_path / "glossary.yaml"
    glossary_file.write_text("- id: living_room\n  en: living room\n  ru: [гостиная, гостиной]\n", encoding="utf-8")
    return LanguageAdapter(
        client=mt,
        glossary=Glossary.load(glossary_file),
        languages=["en", "ru", "et"],
        default_language="en",
        min_confidence=0.5,
    )


def _sse_events(text):
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            events.append(json.loads(frame[len("data:"):].strip()))
    return events


def test_app_builds_a_noop_adapter_when_the_layer_is_off():
    app = create_app(_settings())
    with TestClient(app) as client:
        assert isinstance(client.app.state.language, NoopLanguageAdapter)


def test_chat_translates_in_and_out(tmp_path):
    mt = FakeMT([
        MT("turn off the light in the [E1]", src="ru"),
        MT("Выключил свет в [E1]", src="en"),
    ])
    agent = FakeAgent(reply="Turned off the light in the living room.")
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = agent
        client.app.state.language = _adapter(mt, tmp_path)
        resp = client.post("/api/chat", json={"message": "выключи свет в гостиной"})

    body = resp.json()
    assert agent.seen == ["turn off the light in the living room"]
    assert body["reply"] == "Выключил свет в гостиная"
    assert body["reply_en"] == "Turned off the light in the living room."
    assert body["language"] == "ru"


def test_chat_is_unchanged_when_the_layer_is_off():
    agent = FakeAgent(reply="hi there")
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = agent
        resp = client.post("/api/chat", json={"message": "hello"})

    body = resp.json()
    assert body["reply"] == "hi there"
    assert body["language"] == "en"
    assert body["reply_en"] is None


def test_stream_sends_english_tokens_and_a_translated_done(tmp_path):
    mt = FakeMT([
        MT("turn off the light in the [E1]", src="ru"),
        MT("Выключил свет в [E1]", src="en"),
    ])
    agent = FakeAgent(reply="Turned off the light in the living room.")
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = agent
        client.app.state.language = _adapter(mt, tmp_path)
        resp = client.post("/api/chat/stream", json={"message": "выключи свет в гостиной"})

    events = _sse_events(resp.text)
    tokens = [e for e in events if e["type"] == "token"]
    done = [e for e in events if e["type"] == "done"][0]

    assert agent.seen == ["turn off the light in the living room"]
    assert tokens[0]["text"] == "Turned off the light in the living room."
    assert done["reply"] == "Выключил свет в гостиная"
    assert done["reply_en"] == "Turned off the light in the living room."
    assert done["language"] == "ru"


def test_stream_done_is_unchanged_when_the_layer_is_off():
    agent = FakeAgent(reply="hi there")
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = agent
        resp = client.post("/api/chat/stream", json={"message": "hello"})

    done = [e for e in _sse_events(resp.text) if e["type"] == "done"][0]
    assert done["reply"] == "hi there"
    assert done["language"] == "en"


# --- history overlay --------------------------------------------------------
class FakeStateAgent(FakeAgent):
    """Replays the English transcript the checkpointer would hold."""

    def __init__(self, reply, transcript):
        super().__init__(reply)
        self._transcript = transcript

    async def aget_state(self, config):
        from types import SimpleNamespace
        return SimpleNamespace(values={"messages": self._transcript})


def test_history_shows_native_text_after_a_translated_turn(tmp_path):
    from langchain_core.messages import HumanMessage

    mt = FakeMT([
        MT("turn off the light in the [E1]", src="ru"),
        MT("Выключил свет в [E1]", src="en"),
    ])
    reply_en = "Turned off the light in the living room."
    agent = FakeStateAgent(
        reply_en,
        [HumanMessage("turn off the light in the living room"), AIMessage(reply_en)],
    )
    app = create_app(_settings(language_layer_enabled=True,
                               checkpoint_db_path=str(tmp_path / "checkpoints.sqlite")))
    with TestClient(app) as client:
        client.app.state.agent = agent
        client.app.state.language = _adapter(mt, tmp_path)
        client.post("/api/chat", json={"message": "выключи свет в гостиной", "thread_id": "t1"})
        resp = client.get("/api/history", params={"thread_id": "t1"})

    assert resp.json()["messages"] == [
        {"role": "user", "text": "выключи свет в гостиной"},
        {"role": "bot", "text": "Выключил свет в гостиная"},
    ]


def test_history_is_english_when_the_layer_is_off(tmp_path):
    from langchain_core.messages import HumanMessage

    agent = FakeStateAgent("hi there", [HumanMessage("hello"), AIMessage("hi there")])
    app = create_app(_settings(checkpoint_db_path=str(tmp_path / "checkpoints.sqlite")))
    with TestClient(app) as client:
        client.app.state.agent = agent
        client.post("/api/chat", json={"message": "hello", "thread_id": "t1"})
        resp = client.get("/api/history", params={"thread_id": "t1"})

    assert resp.json()["messages"] == [
        {"role": "user", "text": "hello"},
        {"role": "bot", "text": "hi there"},
    ]


def test_stream_turns_are_recorded_for_history(tmp_path):
    from langchain_core.messages import HumanMessage

    mt = FakeMT([
        MT("turn off the light in the [E1]", src="ru"),
        MT("Выключил свет в [E1]", src="en"),
    ])
    reply_en = "Turned off the light in the living room."
    agent = FakeStateAgent(
        reply_en,
        [HumanMessage("turn off the light in the living room"), AIMessage(reply_en)],
    )
    app = create_app(_settings(language_layer_enabled=True,
                               checkpoint_db_path=str(tmp_path / "checkpoints.sqlite")))
    with TestClient(app) as client:
        client.app.state.agent = agent
        client.app.state.language = _adapter(mt, tmp_path)
        client.post("/api/chat/stream", json={"message": "выключи свет в гостиной", "thread_id": "t1"})
        resp = client.get("/api/history", params={"thread_id": "t1"})

    assert resp.json()["messages"][0]["text"] == "выключи свет в гостиной"


def test_no_overlay_without_a_durable_checkpointer(tmp_path):
    """In-memory threads die on restart anyway — nothing to overlay."""
    app = create_app(_settings(language_layer_enabled=True))
    with TestClient(app) as client:
        assert client.app.state.lang_overlay is None
