"""Builds the agent: LLM + tier-filtered tools + middleware that keeps a
small model healthy (history trimmed to a token budget, current time injected
into the system prompt on every call)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.memory import MemorySaver

from app.agent.llm import build_llm
from app.agent.tool_router import select_tools
from app.config import Settings
from app.skills import list_skills
from app.tools import registry
from app.tools.adapter import LoopGuard, build_tools
from app.tools.context import ToolContext

log = logging.getLogger("agent.factory")

# Reserve room for the model's own output and for tool schemas + prompt.
_RESPONSE_AND_SCHEMA_MARGIN = 2048


def build_system_prompt(settings: Settings, skills_dir: Path) -> str:
    prompt = settings.system_prompt
    metas = list_skills(skills_dir)
    if metas:
        lines = "\n".join(f"- {m.name}: {m.description}" for m in metas)
        prompt += (
            "\n\nSKILL PLAYBOOKS — step-by-step guides for TROUBLESHOOTING and DIAGNOSIS "
            "only (e.g. an automation didn't fire, a device stopped responding). "
            "ONLY when the request is to diagnose or troubleshoot a problem, make your "
            "first tool call load_skill(name) using a name exactly from this list. "
            "For ordinary requests — listing things, checking states or history, running "
            "or toggling devices — do NOT load a skill; use the other tools directly:\n"
            + lines
        )
    if settings.max_tier >= 2:
        domains = ", ".join(settings.allowed_domains)
        prompt += (
            "\n\nYou can also turn entities on or off, toggle them, and trigger "
            f"automations — but only in these domains: {domains}. "
            "Refuse control requests outside them."
        )
    return prompt


def timestamped_system(base_prompt: str) -> SystemMessage:
    now = datetime.now().astimezone()
    return SystemMessage(
        f"{base_prompt}\n\nCurrent time: {now.isoformat(timespec='seconds')} ({now.tzname()})"
    )


def _drop_reasoning(messages: list) -> list:
    """Normalize AI messages before they're replayed in history.

    Thinking-enabled models (Qwen3.5, Claude extended-thinking, etc.) may store
    reasoning tokens in two places:
    - additional_kwargs["reasoning_content"] — stripped here
    - content as a list of {"type":"thinking",...} blocks — flattened to a plain
      string here, keeping only text blocks (tool_calls live in their own field)

    Most inference endpoints (including oMLX) reject both forms when they appear
    in replayed history.
    """
    result = []
    for m in messages:
        if not isinstance(m, AIMessage):
            result.append(m)
            continue
        updates: dict = {}
        if m.additional_kwargs.get("reasoning_content"):
            updates["additional_kwargs"] = {
                k: v for k, v in m.additional_kwargs.items()
                if k != "reasoning_content"
            }
        if isinstance(m.content, list):
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in m.content
                if not (isinstance(block, dict) and block.get("type") == "thinking")
            )
            updates["content"] = text
        if updates:
            m = m.model_copy(update=updates)
        result.append(m)
    return result


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


def latest_human_text(messages: list) -> str:
    """The most recent user message as plain text — the routing signal. Stays
    constant across a run's model calls (the human turn doesn't change while the
    agent loops), so the tool subset is stable within a run."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "human":
            content = m.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # multimodal: concatenate text parts
                return " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
    return ""


class ToolSubsetMiddleware(AgentMiddleware):
    """Narrow the offered tool menu to the latest user message (see tool_router).
    Availability only — the model still chooses among what's offered."""

    def _prepare(self, request):
        text = latest_human_text(request.messages)
        if not text:
            return request
        tools = select_tools(list(request.tools), text)
        if not tools or len(tools) == len(request.tools):
            return request  # nothing to trim
        try:
            return replace(request, tools=tools)
        except TypeError:  # pragma: no cover — future langchain may drop dataclass
            return request.override(tools=tools)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))


class LoopGuardResetMiddleware(AgentMiddleware):
    """The guard dedupes within one run; a fresh run must start clean."""

    def __init__(self, guard: LoopGuard):
        super().__init__()
        self._guard = guard

    def before_agent(self, state, runtime) -> dict | None:
        self._guard.reset()
        return None

    async def abefore_agent(self, state, runtime) -> dict | None:
        self._guard.reset()
        return None


class ContextWindowMiddleware(AgentMiddleware):
    """Non-destructive per-call trimming (the checkpointed history is left
    intact) + fresh timestamp in the system prompt."""

    def __init__(self, base_prompt: str, max_tokens: int):
        super().__init__()
        self._base_prompt = base_prompt
        self._max_tokens = max_tokens

    def _prepare(self, request):
        messages = _drop_reasoning(trim_history(list(request.messages), self._max_tokens))
        system = timestamped_system(self._base_prompt)
        try:
            return replace(request, messages=messages, system_message=system)
        except TypeError:  # pragma: no cover — future langchain versions may swap the dataclass for .override()
            return request.override(messages=messages, system_message=system)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        prepared = self._prepare(request)
        try:
            return await handler(prepared)
        except Exception as exc:
            if "XML syntax error" not in str(exc):
                raise
            log.warning("LLM produced malformed XML tool call — skipping retry: %s", exc)
            return AIMessage(
                content="Your previous tool call contained malformed XML and could not be parsed. "
                "Please retry with well-formed XML."
            )


def build_middleware(settings: Settings, guard: LoopGuard, base_prompt: str, budget: int) -> list[AgentMiddleware]:
    middleware: list[AgentMiddleware] = [
        ContextWindowMiddleware(base_prompt, budget),
        LoopGuardResetMiddleware(guard),
    ]
    if settings.enable_tool_subsetting:
        # First so it trims the menu before the model call is assembled.
        middleware.insert(0, ToolSubsetMiddleware())
    return middleware


def build_agent(settings: Settings, ctx: ToolContext, checkpointer=None):
    if not registry.tools_for_tier(2):  # nothing registered yet
        registry.load_all()
    llm = build_llm(settings)
    guard = LoopGuard()
    tools = build_tools(ctx, settings.max_tier, guard=guard)
    base_prompt = build_system_prompt(settings, ctx.skills_dir)
    budget = max(1024, settings.num_ctx - settings.num_predict - _RESPONSE_AND_SCHEMA_MARGIN)
    return create_agent(
        model=llm,
        tools=tools,
        middleware=build_middleware(settings, guard, base_prompt, budget),
        checkpointer=checkpointer or MemorySaver(),
    )
