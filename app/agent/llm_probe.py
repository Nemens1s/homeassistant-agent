"""Works out llm_provider (and llama.cpp's real context size) from the endpoint,
so users only give a URL and a model. Never raises: on any problem it falls
back to the plain OpenAI-compatible client."""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger("agent.llm")

_PROBE_TIMEOUT = 3.0


async def _get_json(client: httpx.AsyncClient, path: str):
    try:
        response = await client.get(path, timeout=_PROBE_TIMEOUT)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _n_ctx(props: dict) -> int | None:
    settings = props.get("default_generation_settings") or {}
    value = settings.get("n_ctx") or props.get("n_ctx")
    if isinstance(value, int) and value > 0:
        return value
    return None


async def resolve_llm_settings(cfg: Settings, client: httpx.AsyncClient | None = None) -> Settings:
    if cfg.llm_provider != "auto":
        return cfg
    if cfg.api_key:
        return cfg.model_copy(update={"llm_provider": "openai"})
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=cfg.llm_url)
    try:
        props = await _get_json(client, "/props")
        if isinstance(props, dict):
            update = {"llm_provider": "llamacpp"}
            n_ctx = _n_ctx(props)
            if n_ctx is not None:
                update["num_ctx"] = n_ctx
            log.info("llm probe: llama.cpp detected (n_ctx=%s)", n_ctx)
            return cfg.model_copy(update=update)
        tags = await _get_json(client, "/api/tags")
        if isinstance(tags, dict):
            log.info("llm probe: Ollama detected")
            return cfg.model_copy(update={"llm_provider": "ollama"})
        log.warning("llm probe: could not identify %s; using OpenAI-compatible client", cfg.llm_url)
        return cfg.model_copy(update={"llm_provider": "openai"})
    finally:
        if own_client:
            await client.aclose()
