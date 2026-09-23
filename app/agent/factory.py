"""Builds the agent: LLM + tier-filtered tools + middleware that keeps a
small model healthy (history trimmed to a token budget, current time injected
into the system prompt on every call)."""

from __future__ import annotations

import logging
from pathlib import Path

from langchain.agents import create_agent
from app.agent.memory import BoundedMemorySaver

from app.agent.llm import build_llm
from app.agent.middleware import build_middleware
from app.config import Settings
from app.skills import list_skills
from app.tools import registry
from app.tools.adapter import LoopGuard, build_tools
from app.tools.context import ToolContext

log = logging.getLogger("agent.factory")

# Reserve room for the model's own output and for tool schemas + prompt.
_RESPONSE_AND_SCHEMA_MARGIN = 2048

# The single source of truth for choosing BETWEEN tools. Individual tool
# descriptions state only what each tool does and is not for (by intent); the
# cross-tool routing lives here, in one place that sees the whole taxonomy and
# cannot drift as tools are renamed. Kept terse — it rides on every call.
_TOOL_ROUTING = (
    "\n\nCHOOSING A TOOL:\n"
    "- A name/keyword you must resolve to an entity → search_entities to get the "
    "entity_id; if the exact entity_id is already given, get_entity_state directly.\n"
    "- 'What's in <room>' / 'what devices are in <room>' / 'what do I have' → "
    "list_devices (device-level; pass area= for one room). get_areas ONLY for the "
    "list of room names or the whole-home map. Never enumerate individual entities "
    "for a whole room.\n"
    "- The entities/sensors of ONE named device → list_entities(device=). "
    "Filtered fleets ('which lights are on') → list_entities with domain/state.\n"
    "- Battery questions → get_battery_status (already covers every device; no search first).\n"
    "- 'What happened' / 'did automation X fire' across the house → get_activity; "
    "'did device X run?' / state timeline of ONE entity over time → get_history.\n"
    "- Automations (enabled? triggers? config?) → get_automations; diagnosing why "
    "one failed → load_skill."
)


def build_system_prompt(settings: Settings, skills_dir: Path) -> str:
    prompt = settings.system_prompt + _TOOL_ROUTING
    metas = list_skills(skills_dir)
    if metas:
        skill_lines = []
        for meta in metas:
            skill_lines.append(f"- {meta.name}: {meta.description}")
        lines = "\n".join(skill_lines)
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
        prompt += (
            "\n\nACTIONS: real-world actions (turning on lights, starting the vacuum, "
            "etc.) can only be performed by triggering AI-controllable automations — "
            "those whose entity_id starts with 'automation.ai_'. You cannot control "
            "lights, switches, or other entities directly. When the user asks you to "
            "perform an action, first call list_actions to see the available "
            "automations, then trigger the matching one with trigger_action. If none "
            "matches, tell the user the action is not possible yet and that they should "
            "create an automation for it — do not investigate further. Every action "
            "also requires the home's AI-actions switch to be on, or it is refused."
        )
    return prompt


def build_agent(settings: Settings, ctx: ToolContext, checkpointer=None, fast_path=None, telemetry=None):
    """Build the agent.

    Args:
        telemetry: Optional tuple ``(tracer, store_conn)``.  The tracer is
            forwarded to ``TelemetryMiddleware``.  ``store_conn`` is reserved
            for fast-path persistence.
    """
    registry.load_all()
    llm = build_llm(settings)
    guard = LoopGuard()
    tools = build_tools(ctx, settings.max_tier, guard=guard)
    base_prompt = build_system_prompt(settings, ctx.skills_dir)
    budget = max(1024, settings.num_ctx - settings.num_predict - _RESPONSE_AND_SCHEMA_MARGIN)

    telemetry_tracer = None
    telemetry_store_conn = None
    if telemetry is not None:
        telemetry_tracer, telemetry_store_conn = telemetry

    return create_agent(
        model=llm,
        tools=tools,
        middleware=build_middleware(
            settings,
            guard,
            base_prompt,
            budget,
            fast_path=fast_path,
            telemetry_tracer=telemetry_tracer,
            telemetry_store_conn=telemetry_store_conn,
        ),
        checkpointer=checkpointer or BoundedMemorySaver(),
    )
