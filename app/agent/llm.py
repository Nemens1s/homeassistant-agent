"""LLM factory: every backend (llama.cpp's llama-server or any OpenAI-compatible
cloud provider) goes through ChatOpenAI. Everything downstream sees a
BaseChatModel and stays provider-agnostic.

One builder per provider, dispatched by llm_provider. Unknown providers fall back
to the plain OpenAI-compatible builder (safe for cloud APIs)."""

from __future__ import annotations

import httpx
import logging

from langchain_core.language_models.chat_models import BaseChatModel

from app.agent.reasoning_chat_openai import ReasoningChatOpenAI
from app.config import Settings

log = logging.getLogger("agent.llm")

# Generations on modest hardware can exceed a minute, so reads get 300 s. A
# dead or powered-off host must fail in seconds, so connecting gets 3 s —
# never use httpx defaults, which would wait out the OS connect timeout.
LLM_HTTP_TIMEOUT = httpx.Timeout(300.0, connect=3.0)
# One retry rides out a blip; the SDK default (2, with backoff) multiplies an
# outage into minutes.
LLM_MAX_RETRIES = 1


def _build_llamacpp(settings: Settings) -> BaseChatModel:
    """llama.cpp's llama-server: OpenAI-compatible, but also reads min_p/top_k/
    repeat_penalty from the request body. These are not OpenAI-spec, so we forward
    them via extra_body — a strict cloud API would 400 on them, hence llamacpp-only.
    (top_p IS an OpenAI field and rides in the shared kwargs.)"""
    extra_body = {
        "min_p": settings.min_p,
        "top_k": settings.top_k,
        "repeat_penalty": settings.repeat_penalty,
    }
    return ReasoningChatOpenAI(**_openai_compat_kwargs(settings), extra_body=extra_body)


def _openai_compat_kwargs(settings: Settings) -> dict:
    """Kwargs shared by every OpenAI-compatible endpoint (cloud or local).
    Only OpenAI-spec params — provider-specific knobs are added by the caller."""
    log.info(
        "ChatOpenAI params: model=%s url=%s temperature=%s top_p=%s seed=%s max_tokens=%s",
        settings.llm_model, settings.llm_url, settings.temperature, settings.top_p,
        settings.seed, settings.num_predict,
    )
    return {
        "model": settings.llm_model or "model",
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "api_key": settings.api_key or "none",  # some local servers require a non-empty dummy
        "base_url": settings.llm_url or None,
        "max_tokens": settings.num_predict or None,
        "seed": settings.seed,
        "streaming": True,
        "timeout": LLM_HTTP_TIMEOUT,
        "max_retries": LLM_MAX_RETRIES,
    }


def _build_openai_compat(settings: Settings) -> BaseChatModel:
    """Fallback builder for any OpenAI-compatible endpoint — cloud or local.
    Sends only OpenAI-spec params, so it is safe against strict cloud APIs."""
    return ReasoningChatOpenAI(**_openai_compat_kwargs(settings))


# Provider -> builder. Anything not listed falls back to _build_openai_compat.
_BUILDERS = {
    "llamacpp": _build_llamacpp,
}


def build_llm(settings: Settings) -> BaseChatModel:
    builder = _BUILDERS.get(settings.llm_provider, _build_openai_compat)
    return builder(settings)
