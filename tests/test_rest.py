import json

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


async def test_list_states():
    def handler(request):
        assert request.url.path == "/api/states"
        return httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}])

    client = _client(handler)
    data = await client.list_states()
    assert data == [{"entity_id": "light.kitchen", "state": "on"}]
    await client.aclose()


async def test_get_logbook_without_end_time_omits_param():
    def handler(request):
        assert request.url.path == "/api/logbook/2026-07-12T00:00:00"
        assert "end_time" not in request.url.params
        assert "entity_id" not in request.url.params
        return httpx.Response(200, json=[{"when": "2026-07-12T01:00:00+00:00"}])

    client = _client(handler)
    data = await client.get_logbook("2026-07-12T00:00:00")
    assert data == [{"when": "2026-07-12T01:00:00+00:00"}]
    await client.aclose()


async def test_get_logbook_with_entity_id_sends_param():
    def handler(request):
        assert request.url.path == "/api/logbook/2026-07-12T00:00:00"
        assert request.url.params["entity_id"] == "fan.air_purifier"
        return httpx.Response(200, json=[{"when": "2026-07-12T05:00:00+00:00", "entity_id": "fan.air_purifier"}])

    client = _client(handler)
    data = await client.get_logbook("2026-07-12T00:00:00", entity_id="fan.air_purifier")
    assert data[0]["entity_id"] == "fan.air_purifier"
    await client.aclose()


def test_exactly_one_write_method():
    write_like = [n for n in dir(RestClient)
                  if n in ("post", "set_state", "turn_on", "turn_off", "call_service")]
    assert write_like == ["call_service"]


async def test_call_service_posts_and_parses():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/services/light/turn_off"
        assert json.loads(request.content) == {"entity_id": "light.kitchen"}
        return httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "off"}])

    client = RestClient(
        "http://ha.test", "tok", transport=httpx.MockTransport(handler),
        allowed_write_domains=("light", "switch", "automation"),
    )
    data = await client.call_service("light", "turn_off", "light.kitchen")
    assert data[0]["state"] == "off"
    await client.aclose()


async def test_call_service_refuses_non_allowlisted_domain():
    client = RestClient(
        "http://ha.test", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        allowed_write_domains=("light",),
    )
    with pytest.raises(PermissionError):
        await client.call_service("lock", "unlock", "lock.front")
    await client.aclose()


async def test_default_client_cannot_write_at_all():
    client = _client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(PermissionError):
        await client.call_service("light", "turn_on", "light.kitchen")
    await client.aclose()
