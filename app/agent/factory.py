"""Builds the agent: LLM + tier-filtered tools + middleware that keeps a
small model healthy (history trimmed to a token budget, current time injected
into the system prompt on every call)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.memory import MemorySaver

from app.agent.llm import build_llm
from app.config import Settings
from app.skills import list_skills
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.context import ToolContext

# Reserve room for the model's own output and for tool schemas + prompt.
_RESPONSE_AND_SCHEMA_MARGIN = 2048


def build_system_prompt(settings: Settings, skills_dir: Path) -> str:
    prompt = settings.system_prompt
    metas = list_skills(skills_dir)
    if metas:
        lines = "\n".join(f"- {m.name}: {m.description}" for m in metas)
        prompt += (
            "\n\nAvailable skills (playbooks). Call load_skill(name) before "
            "starting a task one of them covers:\n" + lines
        )
    return prompt


def timestamped_system(base_prompt: str) -> SystemMessage:
    now = datetime.now().astimezone()
    return SystemMessage(
        f"{base_prompt}\n\nCurrent time: {now.isoformat(timespec='seconds')} ({now.tzname()})"
    )


def trim_history(messages: list, max_tokens: int) -> list:
    trimmed = trim_messages(
        messages,
        max_tokens=max_tokens,
        token_counter=count_tokens_approximately,
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
    return trimmed or messages[-1:]


class ContextWindowMiddleware(AgentMiddleware):
    """Non-destructive per-call trimming (the checkpointed history is left
    intact) + fresh timestamp in the system prompt."""

    def __init__(self, base_prompt: str, max_tokens: int):
        super().__init__()
        self._base_prompt = base_prompt
        self._max_tokens = max_tokens

    def _prepare(self, request):
        messages = trim_history(list(request.messages), self._max_tokens)
        system = timestamped_system(self._base_prompt)
        try:
            return replace(request, messages=messages, system_message=system)
        except TypeError:  # pragma: no cover — future langchain versions may swap the dataclass for .override()
            return request.override(messages=messages, system_message=system)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))


def build_agent(settings: Settings, ctx: ToolContext, checkpointer=None):
    if not registry.tools_for_tier(2):  # nothing registered yet
        registry.load_all()
    llm = build_llm(settings)
    tools = build_tools(ctx, settings.max_tier)
    base_prompt = build_system_prompt(settings, ctx.skills_dir)
    budget = max(1024, settings.num_ctx - settings.num_predict - _RESPONSE_AND_SCHEMA_MARGIN)
    middleware = ContextWindowMiddleware(base_prompt, budget)
    return create_agent(
        model=llm,
        tools=tools,
        middleware=[middleware],
        checkpointer=checkpointer or MemorySaver(),
    )
