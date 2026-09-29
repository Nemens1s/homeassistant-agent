"""lang-mt HTTP client: request shape, result parsing, fail-open errors."""

import json

import httpx
import pytest

from app.i18n.client import LangMTClient, LangMTError


def _client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://mt.test")
    return LangMTClient("http://mt.test", timeout_s=3.0, client=http, **kwargs)


async def test_translate_posts_the_documented_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "text": "turn off the light",
            "src": "ru",
            "confidence": 0.94,
            "translated": True,
        })

    client = _client(handler)
    result = await client.translate(
        "выключи свет", src="auto", tgt="en", allowed=["en", "ru", "et"], prior="ru"
    )

    body = json.loads(seen["body"])
    assert seen["url"] == "http://mt.test/v1/translate"
    assert body == {
        "text": "выключи свет",
        "src": "auto",
        "tgt": "en",
        "allowed": ["en", "ru", "et"],
        "prior": "ru",
    }
    assert result.text == "turn off the light"
    assert result.src == "ru"
    assert result.confidence == 0.94
    assert result.translated is True


async def test_prior_is_omitted_when_unknown():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "x", "src": "en", "confidence": 1.0, "translated": False})

    client = _client(handler)
    await client.translate("x", src="auto", tgt="en", allowed=["en"], prior=None)

    assert "prior" not in json.loads(seen["body"])


async def test_timeout_raises_mt_timeout():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = _client(handler)
    with pytest.raises(LangMTError) as exc:
        await client.translate("x", src="auto", tgt="en", allowed=["en"])
    assert exc.value.code == "mt_timeout"


async def test_connection_error_raises_mt_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    with pytest.raises(LangMTError) as exc:
        await client.translate("x", src="auto", tgt="en", allowed=["en"])
    assert exc.value.code == "mt_unavailable"


async def test_422_means_a_placeholder_was_lost():
    def handler(request):
        return httpx.Response(422, json={"detail": "placeholder lost"})

    client = _client(handler)
    with pytest.raises(LangMTError) as exc:
        await client.translate("x", src="auto", tgt="en", allowed=["en"])
    assert exc.value.code == "mt_placeholder_lost"


async def test_other_http_errors_raise_mt_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = _client(handler)
    with pytest.raises(LangMTError) as exc:
        await client.translate("x", src="auto", tgt="en", allowed=["en"])
    assert exc.value.code == "mt_error"


async def test_malformed_body_raises_mt_error():
    def handler(request):
        return httpx.Response(200, text="not json")

    client = _client(handler)
    with pytest.raises(LangMTError) as exc:
        await client.translate("x", src="auto", tgt="en", allowed=["en"])
    assert exc.value.code == "mt_error"
