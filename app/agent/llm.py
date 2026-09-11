"""LLM factory: local Ollama by default, any cloud provider via LiteLLM.
Everything downstream sees a BaseChatModel and stays provider-agnostic."""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama

from app.config import Settings

log = logging.getLogger("agent.llm")

# Generations on modest hardware can exceed a minute; never use httpx defaults.
OLLAMA_HTTP_TIMEOUT = 300


def build_llm(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "ollama":
        log.info(
            "ChatOllama params: model=%s num_gpu=%s num_ctx=%s num_predict=%s temperature=%s seed=%s reasoning=%s keep_alive=%s",
            settings.llm_model,settings.num_gpu, settings.num_ctx, settings.num_predict,
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

    from langchain_litellm import ChatLiteLLM  # deferred: heavy import

    if settings.llm_provider == "llamacpp":
        log.info(
            "ChatLiteLLM (llamacpp) params: model=%s url=%s temperature=%s seed=%s max_tokens=%s"
            " — num_ctx/num_gpu/keep_alive/reasoning are server-startup flags, ignored here",
            settings.llm_model, settings.llm_url, settings.temperature, settings.seed, settings.num_predict,
        )
        return ChatLiteLLM(
            model=f"openai/{settings.llm_model or 'model'}",
            temperature=settings.temperature,
            api_key=settings.api_key or "llamacpp",  # dummy; server doesn't authenticate
            api_base=settings.llm_url,
            max_tokens=settings.num_predict,
            streaming=True,
            model_kwargs={"seed": settings.seed},
        )

    return ChatLiteLLM(
        model=f"{settings.llm_provider}/{settings.llm_model}",
        temperature=settings.temperature,
        api_key=settings.api_key or None,
        api_base=settings.llm_url or None,
        streaming=True,
        model_kwargs={"seed": settings.seed},
    )
