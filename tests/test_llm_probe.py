import httpx

from app.agent.llm_probe import resolve_llm_settings
from app.config import Settings


def _client(routes):
    def handler(request):
        fn = routes.get(request.url.path)
        if fn is None:
            return httpx.Response(404)
        return fn(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://llm")


async def test_detects_llamacpp_and_reads_n_ctx():
    c = _client({"/props": lambda r: httpx.Response(
        200, json={"default_generation_settings": {"n_ctx": 32768}})})
    s = await resolve_llm_settings(Settings(_env_file=None, llm_url="http://llm"), c)
    assert s.llm_provider == "llamacpp"
    assert s.num_ctx == 32768


async def test_api_key_means_cloud_without_probing():
    c = _client({})
    s = await resolve_llm_settings(Settings(_env_file=None, api_key="k"), c)
    assert s.llm_provider == "openai"


async def test_explicit_provider_is_kept():
    c = _client({"/props": lambda r: httpx.Response(200, json={})})
    s = await resolve_llm_settings(Settings(_env_file=None, llm_provider="llamacpp"), c)
    assert s.llm_provider == "llamacpp"


async def test_unreachable_falls_back_to_openai_compat():
    def boom(request):
        raise httpx.ConnectError("down")
    c = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://llm")
    s = await resolve_llm_settings(Settings(_env_file=None), c)
    assert s.llm_provider == "openai"
