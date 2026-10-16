"""LLM- and fast-path-specific health pieces: the probes ServiceHealth runs,
the rule for which exceptions mean "the service is down" (as opposed to "the
model answered badly"), and re-resolving an `auto` provider that could not be
identified at boot."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import ollama
import openai

from app.agent.llm_probe import resolve_llm_settings
from app.config import Settings

_PROBE_TIMEOUT = 3.0

Probe = Callable[[], Awaitable[bool]]


def _is_outage(exc: BaseException) -> bool:
    # httpx.TransportError covers connect/read timeouts, refused connections
    # and dropped streams. Ollama's client re-raises httpx.ConnectError as a
    # builtin ConnectionError "from None", so the chain is lost — match it too.
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return True
    # APITimeoutError is a subclass of APIConnectionError.
    if isinstance(exc, (openai.APIConnectionError, openai.InternalServerError)):
        return True
    if isinstance(exc, ollama.ResponseError):
        return getattr(exc, "status_code", -1) >= 500
    return False


def is_llm_outage(exc: BaseException) -> bool:
    """True when *exc* (or anything in its cause/context chain) means the LLM
    server could not be reached or failed server-side. Bad model output,
    validation errors and recursion limits are not outages."""
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _is_outage(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_fast_path_outage(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError)


def _strip_v1(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url[: -len("/v1")]
    return url


def _probe_urls(settings: Settings, unresolved: bool) -> list[str]:
    base = settings.llm_url.rstrip("/")
    root = _strip_v1(base)
    if unresolved:
        # Same endpoints the boot-time probe tried, plus the generic one.
        return [root + "/props", root + "/api/tags", base + "/models"]
    if settings.llm_provider == "llamacpp":
        return [root + "/health"]  # 503 while the model is loading
    if settings.llm_provider == "ollama":
        return [base + "/api/version"]
    return [base + "/models"]


async def _any_ok(client: httpx.AsyncClient, urls: list[str]) -> bool:
    for url in urls:
        try:
            response = await client.get(url, timeout=_PROBE_TIMEOUT)
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return True
    return False


def build_llm_probe(settings: Settings, client: httpx.AsyncClient, unresolved: bool = False) -> Probe:
    urls = _probe_urls(settings, unresolved)

    async def probe() -> bool:
        return await _any_ok(client, urls)

    return probe


def build_fast_path_probe(url: str, client: httpx.AsyncClient) -> Probe:
    urls = [url.rstrip("/") + "/health"]

    async def probe() -> bool:
        return await _any_ok(client, urls)

    return probe


def needs_reresolve(cfg: Settings, run_cfg: Settings) -> bool:
    """True when the boot-time `auto` probe fell back to the OpenAI-compatible
    client: either the server was down, or it really is a plain OpenAI-style
    server. Re-resolving on recovery is harmless in the second case."""
    return cfg.llm_provider == "auto" and not cfg.api_key and run_cfg.llm_provider == "openai"


async def reresolve(cfg: Settings, current: Settings) -> Settings | None:
    """Probe the backend again. Returns the new settings when the provider or
    the context size changed (the agent must be rebuilt), else None."""
    fresh = await resolve_llm_settings(cfg)
    if fresh.llm_provider == current.llm_provider and fresh.num_ctx == current.num_ctx:
        return None
    return fresh
