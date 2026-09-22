"""LLM factory: local Ollama by default, any OpenAI-compatible cloud provider via ChatOpenAI.
Everything downstream sees a BaseChatModel and stays provider-agnostic.

One builder per provider, dispatched by llm_provider. Unknown providers fall back
to the plain OpenAI-compatible builder (safe for cloud APIs)."""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama

from app.agent.reasoning_chat_openai import ReasoningChatOpenAI
from app.config import Settings

log = logging.getLogger("agent.llm")

# Generations on modest hardware can exceed a minute; never use httpx defaults.
OLLAMA_HTTP_TIMEOUT = 300


def _build_ollama(settings: Settings) -> BaseChatModel:
    log.info(
        "ChatOllama params: model=%s num_gpu=%s num_ctx=%s num_predict=%s temperature=%s seed=%s reasoning=%s keep_alive=%s",
        settings.llm_model, settings.num_gpu, settings.num_ctx, settings.num_predict,
        settings.temperature, settings.seed, settings.reasoning, settings.keep_alive,
    )
    return ChatOllama(
        base_url=settings.llm_url,
        model=settings.llm_model,
        temperature=settings.temperature,
        seed=settings.seed,
        reasoning=settings.reasoning,
        num_predict=settings.num_predict,
        num_gpu=settings.num_gpu,
        num_ctx=settings.num_ctx,
        keep_alive=settings.keep_alive,
        client_kwargs={"timeout": OLLAMA_HTTP_TIMEOUT},
    )


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
        "ChatOpenAI params: model=%s url=%s temperature=%s top_p=%s seed=%s max_tokens=%s"
        " — num_ctx/num_gpu/keep_alive/reasoning are Ollama-only, ignored here",
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
    }


def _build_openai_compat(settings: Settings) -> BaseChatModel:
    """Fallback builder for any OpenAI-compatible endpoint — cloud or local.
    Sends only OpenAI-spec params, so it is safe against strict cloud APIs."""
    return ReasoningChatOpenAI(**_openai_compat_kwargs(settings))


# Provider -> builder. Anything not listed falls back to _build_openai_compat.
_BUILDERS = {
    "ollama": _build_ollama,
    "llamacpp": _build_llamacpp,
}


def build_llm(settings: Settings) -> BaseChatModel:
    builder = _BUILDERS.get(settings.llm_provider, _build_openai_compat)
    return builder(settings)
