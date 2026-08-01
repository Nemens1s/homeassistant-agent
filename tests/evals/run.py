"""Tool-selection eval: does the configured model call the right tool with
the right params on the first turn? Usage:

    venv/bin/python -m tests.evals.run            # uses .env settings
    LLM_MODEL=llama3.1:8b venv/bin/python -m tests.evals.run
"""

import argparse
import asyncio
import sys
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage

from app.agent.factory import build_system_prompt, timestamped_system
from app.agent.llm import build_llm
from app.config import load_settings
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.context import ToolContext

CASES_FILE = Path(__file__).parent / "cases.yaml"


def check(case: dict, tool_calls: list) -> tuple[bool, str]:
    banned = case.get("expect_not_tool")
    if banned:
        if tool_calls and tool_calls[0]["name"] == banned:
            return False, f"called banned tool {banned} (args {tool_calls[0]['args']})"
        return True, ""

    if not tool_calls:
        return False, "no tool call"
    call = tool_calls[0]
    if call["name"] != case["expect_tool"]:
        return False, f"called {call['name']} (args {call['args']})"
    for key, expected in (case.get("expect_params") or {}).items():
        actual = call["args"].get(key)
        if actual != expected:
            return False, f"param {key}={actual!r}, expected {expected!r}"
    return True, ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tier", type=int, default=None)
    args = parser.parse_args()

    settings = load_settings()
    max_tier = args.max_tier if args.max_tier is not None else settings.max_tier

    registry._reset_for_tests()
    registry.load_all()
    ctx = ToolContext(settings=settings, rest=None, ws=None)  # handlers never run
    tools = build_tools(ctx, max_tier=max_tier)
    llm = build_llm(settings).bind_tools(tools)
    system = timestamped_system(build_system_prompt(settings, ctx.skills_dir))

    cases = yaml.safe_load(CASES_FILE.read_text())
    passed = 0
    run = 0
    print(f"model: {settings.llm_model} via {settings.llm_provider}\n")
    for case in cases:
        if case.get("min_tier", 1) > max_tier:
            print(f"  SKIP  {case['id']} (needs tier {case['min_tier']})")
            continue
        run += 1
        msg = await llm.ainvoke([system, HumanMessage(case["prompt"])])
        ok, reason = check(case, msg.tool_calls)
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {case['id']}" + (f" — {reason}" if reason else ""))
    print(f"\nscore: {passed}/{run}")
    return 0 if passed == run else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
