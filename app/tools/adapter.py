"""ToolDefinition → LangChain StructuredTool. The single seam where all
cross-cutting behavior lives: validation envelopes, exception → error-code
mapping, per-call audit logging, and the repeated-call loop guard. Handlers
stay pure; nothing below this layer raises into the agent loop.
"""

from __future__ import annotations

import difflib
import json
import logging
import time

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from app.tools import registry
from app.tools.base import ToolDefinition, ToolResult, entity_domain
from app.tools.context import ToolContext

log = logging.getLogger("agent.tools")
log_actions = logging.getLogger("agent.actions")


class LoopGuard:
    """Per-thread dedupe of identical consecutive calls. reset() clears all
    threads — called by the per-run reset middleware; cross-thread resets are
    accepted (false negatives are harmless, dedupe is best-effort)."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[str, str]] = {}

    def is_repeat(self, thread_id: str, name: str, args_json: str) -> bool:
        key = (name, args_json)
        if self._last.get(thread_id) == key:
            return True
        self._last[thread_id] = key
        return False

    def reset(self) -> None:
        self._last.clear()


def _validation_message(exc: ValidationError) -> str:
    problems = []
    for e in exc.errors():
        loc_parts = []
        for p in e["loc"]:
            loc_parts.append(str(p))
        problems.append(f"{'.'.join(loc_parts)}: {e['msg']}")
    return "Invalid parameters — " + "; ".join(problems)


async def _did_you_mean(ctx: ToolContext, entity_id: str) -> list[str]:
    try:
        states = await ctx.rest.list_states()
        ids = []
        for s in states:
            ids.append(s["entity_id"])
        return difflib.get_close_matches(entity_id, ids, n=3, cutoff=0.5)
    except Exception:  # suggestion is best-effort; never mask the real error
        return []


def to_structured_tool(
    defn: ToolDefinition, ctx: ToolContext, guard: LoopGuard
) -> StructuredTool:
    # A tool may derive its schema from the runtime context (e.g. an enum built
    # from files); fall back to the static model otherwise.
    params_model = defn.dynamic_params(ctx) if defn.dynamic_params else defn.params_model

    async def _run(config: RunnableConfig = None, **kwargs) -> str:
        started = time.monotonic()
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "default")
        args_json = json.dumps(kwargs, sort_keys=True, default=str)
        if guard.is_repeat(thread_id, defn.name, args_json):
            result = ToolResult.error(
                "repeated_call",
                "You already called this tool with identical arguments. "
                "Use the previous result or try a different approach.",
            )
        else:
            try:
                params = params_model(**kwargs)
                result = await defn.handler(params, ctx)
            except ValidationError as exc:
                result = ToolResult.error("invalid_params", _validation_message(exc))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    entity_id = str(kwargs.get("entity_id", ""))
                    data = (
                        {"did_you_mean": await _did_you_mean(ctx, entity_id)}
                        if entity_id
                        else None
                    )
                    result = ToolResult.error(
                        "entity_not_found", f"No entity {entity_id!r}.", data=data
                    )
                else:
                    result = ToolResult.error(
                        "ha_unreachable",
                        f"Home Assistant returned HTTP {exc.response.status_code}.",
                    )
            except (httpx.HTTPError, ConnectionError) as exc:
                result = ToolResult.error(
                    "ha_unreachable", f"Could not reach Home Assistant: {exc}."
                )
            except TimeoutError:
                result = ToolResult.error(
                    "ha_timeout", "Home Assistant did not answer in time."
                )
            except PermissionError as exc:
                result = ToolResult.error("domain_not_allowed", str(exc))
            except RuntimeError as exc:
                result = ToolResult.error(
                    "ha_error", f"Command failed: {exc}."
                )
            except Exception as exc:  # terminal guard: nothing may escape into the agent loop
                log.exception("tool=%s unexpected error", defn.name)
                result = ToolResult.error("internal_error", f"Unexpected error: {exc}.")
        duration_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "tool=%s tier=%s status=%s duration_ms=%s args=%s",
            defn.name, int(defn.tier), result.status, duration_ms, args_json,
        )
        if defn.tier >= 2:
            entity_id = str(kwargs.get("entity_id", ""))
            domain = entity_domain(entity_id) if "." in entity_id else ""
            service = str(kwargs.get("action", "")) or defn.name
            log_actions.info(
                "action tool=%s domain=%s service=%s entity=%s status=%s",
                defn.name, domain, service, entity_id, result.status,
            )
            if ctx.audit is not None:
                await ctx.audit.record(
                    thread_id=thread_id, tool=defn.name, entity_id=entity_id,
                    domain=domain, service=service, params_json=args_json,
                    status=result.status, error_code=result.error_code,
                    duration_ms=duration_ms,
                )
        return result.to_json()

    def _handle_validation_error(exc):
        return ToolResult.error("invalid_params", _validation_message(exc)).to_json()

    return StructuredTool(
        name=defn.name,
        description=defn.description,
        args_schema=params_model,
        coroutine=_run,
        handle_validation_error=_handle_validation_error,
    )


def build_tools(ctx: ToolContext, max_tier: int, guard: LoopGuard | None = None) -> list[StructuredTool]:
    guard = guard or LoopGuard()
    tools = []
    for d in registry.tools_for_tier(max_tier):
        tools.append(to_structured_tool(d, ctx, guard))
    return tools
