"""LLM factory: local Ollama by default, any cloud provider via LiteLLM.
Everything downstream sees a BaseChatModel and stays provider-agnostic."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama

from app.config import Settings

# Generations on modest hardware can exceed a minute; never use httpx defaults.
OLLAMA_HTTP_TIMEOUT = 300


def build_llm(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "ollama":
        return ChatOllama(
            base_url=settings.ollama_url,
            model=settings.llm_model,
            temperature=settings.temperature,
            seed=settings.seed,
            reasoning=settings.reasoning,
            num_predict=settings.num_predict,
            num_ctx=settings.num_ctx,
            keep_alive=settings.keep_alive,
            client_kwargs={"timeout": OLLAMA_HTTP_TIMEOUT},
        )

    from langchain_litellm import ChatLiteLLM  # deferred: heavy import

    return ChatLiteLLM(
        model=f"{settings.llm_provider}/{settings.llm_model}",
        temperature=settings.temperature,
        api_key=settings.api_key or None,
    )
