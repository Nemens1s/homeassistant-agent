import httpx
import openai
from langgraph.errors import GraphRecursionError

from app.agent import llm_health
from app.agent.llm_health import (
    build_fast_path_probe,
    build_llm_probe,
    is_fast_path_outage,
    is_llm_outage,
    needs_reresolve,
    reresolve,
)
from app.config import Settings


def _client(routes):
    """routes: path -> status code, or an Exception to raise."""
    def handler(request):
        outcome = routes.get(request.url.path)
        if outcome is None:
            return httpx.Response(404)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings(**values):
    return Settings(_env_file=None, **values)


async def test_llamacpp_probe_uses_health_without_v1():
    probe = build_llm_probe(
        _settings(llm_provider="llamacpp", llm_url="http://llm:8080/v1"),
        _client({"/health": 200}),
    )
    assert await probe() is True


async def test_llamacpp_loading_model_counts_as_down():
    probe = build_llm_probe(
        _settings(llm_provider="llamacpp", llm_url="http://llm:8080"),
        _client({"/health": 503}),
    )
    assert await probe() is False


async def test_openai_compatible_probe_uses_models():
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="https://api.example.com/v1"),
        _client({"/v1/models": 200}),
    )
    assert await probe() is True


async def test_unresolved_probe_accepts_any_known_endpoint():
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="http://llm:8080"),
        _client({"/props": 200}),
        unresolved=True,
    )
    assert await probe() is True


async def test_probe_connection_error_is_down():
    probe = build_llm_probe(
        _settings(llm_provider="llamacpp", llm_url="http://llm:8080"),
        _client({"/health": httpx.ConnectError("refused")}),
    )
    assert await probe() is False


async def test_fast_path_probe():
    up = build_fast_path_probe("http://needle:8000/", _client({"/health": 200}))
    down = build_fast_path_probe("http://needle:8000", _client({"/health": httpx.ConnectError("x")}))
    assert await up() is True
    assert await down() is False


def test_is_llm_outage_true_cases():
    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    cases = [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ConnectionError("refused"),
        openai.APIConnectionError(request=request),
        openai.APITimeoutError(request=request),
        openai.InternalServerError("boom", response=httpx.Response(500, request=request), body=None),
    ]
    for exc in cases:
        assert is_llm_outage(exc), exc


def test_is_llm_outage_false_cases():
    for exc in (ValueError("bad output"), GraphRecursionError("limit")):
        assert not is_llm_outage(exc), exc


def test_is_llm_outage_follows_exception_chain():
    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_llm_outage(outer)


def test_is_fast_path_outage():
    assert is_fast_path_outage(httpx.ConnectError("x"))
    assert not is_fast_path_outage(ValueError("bad json"))


def test_needs_reresolve():
    auto = _settings()
    assert needs_reresolve(auto, auto.model_copy(update={"llm_provider": "openai"})) is True
    assert needs_reresolve(auto, auto.model_copy(update={"llm_provider": "llamacpp"})) is False
    keyed = _settings(api_key="k")
    assert needs_reresolve(keyed, keyed.model_copy(update={"llm_provider": "openai"})) is False
    explicit = _settings(llm_provider="llamacpp")
    assert needs_reresolve(explicit, explicit) is False


async def test_reresolve_returns_new_settings_only_when_changed(monkeypatch):
    cfg = _settings()
    current = cfg.model_copy(update={"llm_provider": "openai"})

    async def to_llamacpp(c, client=None):
        return c.model_copy(update={"llm_provider": "llamacpp"})

    monkeypatch.setattr(llm_health, "resolve_llm_settings", to_llamacpp)
    fresh = await reresolve(cfg, current)
    assert fresh is not None
    assert fresh.llm_provider == "llamacpp"

    async def still_openai(c, client=None):
        return c.model_copy(update={"llm_provider": "openai"})

    monkeypatch.setattr(llm_health, "resolve_llm_settings", still_openai)
    assert await reresolve(cfg, current) is None


def _recording_client(status, seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_probe_401_with_api_key_is_up_and_sends_bearer():
    seen = []
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="https://api.example.com/v1", api_key="sk-test"),
        _recording_client(401, seen),
    )
    assert await probe() is True
    assert seen[0].headers.get("authorization") == "Bearer sk-test"


async def test_probe_without_api_key_sends_no_authorization():
    seen = []
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="https://api.example.com/v1"),
        _recording_client(200, seen),
    )
    assert await probe() is True
    assert "authorization" not in seen[0].headers


async def test_probe_404_counts_as_up():
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="https://api.example.com/v1"),
        _client({}),
    )
    assert await probe() is True


async def test_probe_500_is_down():
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="https://api.example.com/v1"),
        _client({"/v1/models": 500}),
    )
    assert await probe() is False


async def test_unresolved_probe_connection_errors_everywhere_is_down():
    error = httpx.ConnectError("refused")
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="http://llm:8080"),
        _client({"/props": error, "/api/tags": error, "/models": error}),
        unresolved=True,
    )
    assert await probe() is False
