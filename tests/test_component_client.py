import aiohttp
import pytest
from aiohttp import web

from custom_components.agent_gosling.client import AgentApiClient, AgentApiError


@pytest.fixture
async def fake_app(aiohttp_server_factory=None):
    async def chat(request):
        body = await request.json()
        if body["message"] == "boom":
            return web.Response(status=500)
        return web.json_response({"reply": f"echo:{body['message']}:{body['thread_id']}"})

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def test_chat_roundtrip(fake_app):
    async with aiohttp.ClientSession() as session:
        client = AgentApiClient(fake_app, session)
        reply = await client.chat("hello", "conv-1")
    assert reply == "echo:hello:conv-1"


async def test_http_error_raises_agent_api_error(fake_app):
    async with aiohttp.ClientSession() as session:
        client = AgentApiClient(fake_app, session)
        with pytest.raises(AgentApiError):
            await client.chat("boom", "conv-1")


async def test_unreachable_raises_agent_api_error():
    async with aiohttp.ClientSession() as session:
        client = AgentApiClient("http://127.0.0.1:59997", session, timeout=1.0)
        with pytest.raises(AgentApiError):
            await client.chat("hello", "conv-1")
