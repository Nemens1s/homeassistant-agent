"""Tool-selection eval: does the configured model call the right tool with
the right params on the first turn? Usage:

    venv/bin/python -m tests.evals.run            # uses .env settings
    LLM_MODEL=llama3.1:8b venv/bin/python -m tests.evals.run
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage
from langsmith.integrations import otel

from app.agent.factory import build_system_prompt, timestamped_system
from app.agent.llm import build_llm
from app.agent.tool_router import select_tools
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
        # Case-insensitive for strings: HA area/friendly names vary in case and the
        # handlers match case-insensitively, so "Bedroom" == "bedroom" is a pass.
        if isinstance(actual, str) and isinstance(expected, str):
            if actual.lower() != expected.lower():
                return False, f"param {key}={actual!r}, expected {expected!r}"
        elif actual != expected:
            return False, f"param {key}={actual!r}, expected {expected!r}"
    return True, ""

def _write_to_file(output: dict) -> None:
    title = f"{datetime.now().isoformat(timespec='seconds')} {output['model']} {output['provider']}"
    out_dir = Path(__file__).parent.parent.parent / ".evals"
    out_dir.mkdir(exist_ok=True)
    filename = title.replace(":", "-").replace(" ", "_").replace("/", "-") + ".json"
    (out_dir / filename).write_text(json.dumps(output, indent=2))

async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tier", type=int, default=None)
    parser.add_argument("--save-results", action="store_true", help="Save test results to a file")
    args = parser.parse_args()

    settings = load_settings()
    max_tier = args.max_tier if args.max_tier is not None else settings.max_tier
    save_results = True if args.save_results else False

    registry._reset_for_tests()
    registry.load_all()
    ctx = ToolContext(settings=settings, rest=None, ws=None)  # handlers never run
    tools = build_tools(ctx, max_tier=max_tier)
    llm = build_llm(settings)
    system = timestamped_system(build_system_prompt(settings, ctx.skills_dir))
    output = {'model': settings.llm_model,
              'provider': settings.llm_provider,
              'num_gpu': settings.num_gpu,
              'num_ctx': settings.num_ctx}

    cases = yaml.safe_load(CASES_FILE.read_text())
    passed = 0
    run = 0
    subset = settings.enable_tool_subsetting
    print(f"model: {settings.llm_model} via {settings.llm_provider} "
          f"(tool_subsetting={'on' if subset else 'off'})\n")
    t0 = time.monotonic()
    for case in cases:
        if case.get("min_tier", 1) > max_tier:
            print(f"  SKIP  {case['id']} (needs tier {case['min_tier']})")
            continue
        run += 1
        # Mirror production: the agent only sees the per-query tool subset.
        case_tools = select_tools(tools, case["prompt"]) if subset else tools
        bound = llm.bind_tools(case_tools)
        msg = await bound.ainvoke([system, HumanMessage(case["prompt"])])
        ok, reason = check(case, msg.tool_calls)
        passed += ok
        output_message = f"  {'PASS' if ok else 'FAIL'}  {case['id']}" + (f" — {reason}" if reason else "")
        output[case['id']] = output_message
        print(output_message)
    elapsed = time.monotonic() - t0
    output['elapsed'] =f"\n[{elapsed:.1f}s]\n"
    print(f"\nscore: {passed}/{run}")
    print(f"\n[{elapsed:.1f}s]\n")
    if save_results:
        _write_to_file(output)
    return 0 if passed == run else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
