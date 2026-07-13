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
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from app.tools import registry
from app.tools.base import ToolDefinition, ToolResult
from app.tools.context import ToolContext

log = logging.getLogger("agent.tools")


class LoopGuard:
    """Shared across one agent's tools: flags an identical consecutive call."""

    def __init__(self) -> None:
        self._last: tuple[str, str] | None = None

    def is_repeat(self, name: str, args_json: str) -> bool:
        key = (name, args_json)
        if key == self._last:
            return True
        self._last = key
        return False


def _validation_message(exc: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
    ]
    return "Invalid parameters — " + "; ".join(problems)


async def _did_you_mean(ctx: ToolContext, entity_id: str) -> list[str]:
    try:
        states = await ctx.rest.list_states()
        ids = [s["entity_id"] for s in states]
        return difflib.get_close_matches(entity_id, ids, n=3, cutoff=0.5)
    except Exception:  # suggestion is best-effort; never mask the real error
        return []


def to_structured_tool(
    defn: ToolDefinition, ctx: ToolContext, guard: LoopGuard
) -> StructuredTool:
    async def _run(**kwargs) -> str:
        args_json = json.dumps(kwargs, sort_keys=True, default=str)
        if guard.is_repeat(defn.name, args_json):
            result = ToolResult.error(
                "repeated_call",
                "You already called this tool with identical arguments. "
                "Use the previous result or try a different approach.",
            )
            log.info("tool=%s status=error duration_ms=0 args=%s", defn.name, args_json)
            return result.to_json()

        started = time.monotonic()
        try:
            params = defn.params_model(**kwargs)
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
        duration_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "tool=%s status=%s duration_ms=%s args=%s",
            defn.name, result.status, duration_ms, args_json,
        )
        return result.to_json()

    return StructuredTool(
        name=defn.name,
        description=defn.description,
        args_schema=defn.params_model,
        coroutine=_run,
        handle_validation_error=lambda exc: ToolResult.error(
            "invalid_params", _validation_message(exc)
        ).to_json(),
    )


def build_tools(ctx: ToolContext, max_tier: int) -> list[StructuredTool]:
    guard = LoopGuard()
    return [to_structured_tool(d, ctx, guard) for d in registry.tools_for_tier(max_tier)]
