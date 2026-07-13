import httpx
import pytest

from app.ha.rest import RestClient


def _client(handler):
    return RestClient("http://ha.test", "tok", transport=httpx.MockTransport(handler))


async def test_get_state_sends_auth_and_parses():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.url.path == "/api/states/light.kitchen"
        return httpx.Response(200, json={"entity_id": "light.kitchen", "state": "on"})

    client = _client(handler)
    data = await client.get_state("light.kitchen")
    assert data["state"] == "on"
    await client.aclose()


async def test_get_state_raises_on_404():
    client = _client(lambda req: httpx.Response(404, json={"message": "not found"}))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_state("light.nope")
    await client.aclose()


async def test_get_history_params():
    def handler(request):
        assert request.url.path == "/api/history/period/2026-07-12T00:00:00"
        assert request.url.params["filter_entity_id"] == "sensor.temp"
        assert request.url.params["end_time"] == "2026-07-13T00:00:00"
        return httpx.Response(200, json=[[{"state": "21.5"}]])

    client = _client(handler)
    data = await client.get_history("sensor.temp", "2026-07-12T00:00:00", "2026-07-13T00:00:00")
    assert data == [[{"state": "21.5"}]]
    await client.aclose()


async def test_ping():
    client = _client(lambda req: httpx.Response(200, json={"message": "API running."}))
    assert await client.ping() is True
    await client.aclose()


async def test_error_log_returns_text():
    client = _client(lambda req: httpx.Response(200, text="line1\nline2"))
    assert await client.get_error_log() == "line1\nline2"
    await client.aclose()


def test_no_write_methods_exist():
    banned = ("post", "call_service", "set_state", "turn_on", "turn_off")
    for name in banned:
        assert not hasattr(RestClient, name)
