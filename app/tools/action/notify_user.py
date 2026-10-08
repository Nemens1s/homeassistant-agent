"""The one way the agent tells the user something outside the chat.

The agent always writes English and always "sends". Python decides the rest,
invisible to the agent: the text is translated into the user's language, and
when the run scope says notifications are suppressed (event confirmations
switched off) nothing is sent — but the envelope is exactly what a real send
returns, so the agent's behaviour never depends on the toggle.
"""

import logging

from pydantic import BaseModel, Field

from app.agent.run_scope import current_scope
from app.constants import AI_SCRIPT_PREFIX
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register

log_actions = logging.getLogger("agent.actions")


class Params(BaseModel):
    message: str = Field(
        max_length=500,
        description="What to tell the user, in plain English, one or two short sentences.",
    )
    title: str | None = Field(None, max_length=80, description="Optional short title.")


async def handler(params: Params, ctx) -> ToolResult:
    target = ctx.settings.notify_action
    if not target.startswith(AI_SCRIPT_PREFIX):
        return ToolResult.error(
            "not_configured",
            f"notify_action {target!r} must be an AI-controllable script ({AI_SCRIPT_PREFIX}*).",
        )
    if "script" not in ctx.settings.allowed_domains:
        return ToolResult.error("domain_not_allowed", "The 'script' domain is not in the allowed list.")

    scope = current_scope()
    if scope.suppress_notify:
        log_actions.info("notify suppressed thread=%s message=%r", scope.thread_id, params.message)
        return ToolResult.ok({"sent": True})

    data = {"message": await ctx.lang.from_english(params.message, scope.language)}
    if params.title:
        data["title"] = await ctx.lang.from_english(params.title, scope.language)
    await ctx.rest.call_service("script", target.split(".", 1)[1], data=data)
    return ToolResult.ok({"sent": True})


register(
    ToolDefinition(
        name="notify_user",
        description=(
            "Send the user a short push notification (plain English; it is translated "
            "for them). Use to deliver a reminder or to report what you did after an "
            "[EVENT]. Not for chat replies."
        ),
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
