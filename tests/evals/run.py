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
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from app.agent.factory import build_agent, build_system_prompt, timestamped_system
from app.agent.llm import build_llm
from app.agent.tool_router import select_tools
from app.config import load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
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

    # Accept expect_tool (str) or expect_any_tool (list) — both are valid.
    any_of = case.get("expect_any_tool")
    expected = any_of or [case.get("expect_tool")]
    if call["name"] not in expected:
        return False, f"called {call['name']} (args {call['args']})"

    # Skip param check when the model called an alternate "discovery" tool that
    # naturally takes no args (e.g. list_skills called instead of load_skill).
    if any_of and call["name"] != case.get("expect_tool"):
        return True, ""

    # Case-insensitive param check: HA area/friendly names vary in case and the
    # handlers match case-insensitively, so "Bedroom" == "bedroom" is a pass.
    for key, exp_val in (case.get("expect_params") or {}).items():
        actual = call["args"].get(key)
        if isinstance(actual, str) and isinstance(exp_val, str):
            if actual.lower() != exp_val.lower():
                return False, f"param {key}={actual!r}, expected {exp_val!r}"
        elif actual != exp_val:
            return False, f"param {key}={actual!r}, expected {exp_val!r}"
    return True, ""

def _write_to_file(output: dict) -> None:
    title = f"{datetime.now().isoformat(timespec='seconds')} {output['model']} {output['provider']}"
    out_dir = Path(__file__).parent.parent.parent / ".evals"
    out_dir.mkdir(exist_ok=True)
    filename = title.replace(":", "-").replace(" ", "_").replace("/", "-") + ".json"
    (out_dir / filename).write_text(json.dumps(output, indent=2))

async def _agent_trace(settings, prompt: str) -> list[dict]:
    """Run the full agent against real HA and capture every turn as a list of dicts.
    Each entry is one of:
      {"thinking": ..., "calls": [...]}   — AI turn with tool proposals
      {"tool_result": "..."}              — tool response
      {"thinking": ..., "answer": "..."}  — final AI turn with text reply
      {"error": "..."}                    — agent raised an exception
    """
    write_domains = tuple(settings.allowed_domains) if settings.max_tier >= 2 else ()
    rest = RestClient(settings.ha_base_url, settings.ha_token, allowed_write_domains=write_domains)
    ws: WebSocketClient | None = None
    try:
        _ws = WebSocketClient(settings.ws_url, settings.ha_token)
        await _ws.start(connect_timeout=settings.ws_connect_timeout)
        ws = _ws
    except Exception:
        pass

    trace_ctx = ToolContext(settings=settings, rest=rest, ws=ws)
    agent = build_agent(settings, trace_ctx)
    config = {
        "configurable": {"thread_id": f"eval-{time.monotonic_ns()}"},
        "recursion_limit": settings.recursion_limit,
    }

    trace: list[dict] = []
    current: dict = {}
    pending: dict[int, dict] = {}

    try:
        async for token, _ in agent.astream(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            stream_mode="messages",
        ):
            if not isinstance(token, AIMessageChunk):
                if current:
                    trace.append({k: v for k, v in current.items() if v})
                current = {}
                pending = {}
                if isinstance(token, ToolMessage):
                    raw = token.content
                    trace.append({"tool_result": raw if isinstance(raw, str) else json.dumps(raw)})
                continue

            if t := token.additional_kwargs.get("reasoning_content", ""):
                current["thinking"] = current.get("thinking", "") + t

            for chunk in token.tool_call_chunks or []:
                idx = chunk.get("index") or 0
                if idx not in pending:
                    pending[idx] = {"name": "", "args": ""}
                if chunk.get("name"):
                    pending[idx]["name"] += chunk["name"]
                pending[idx]["args"] += chunk.get("args") or ""
            if pending:
                current["calls"] = [
                    {"name": v["name"], "args": json.loads(v["args"]) if v["args"] else {}}
                    for v in pending.values() if v["name"]
                ]

            if txt := (token.content if isinstance(token.content, str) else ""):
                current["answer"] = current.get("answer", "") + txt

        if current:
            trace.append({k: v for k, v in current.items() if v})
    except Exception as exc:
        trace.append({"error": str(exc)})
    finally:
        await rest.aclose()
        if ws:
            await ws.stop()

    return trace


def _print_trace(trace: list[dict]) -> None:
    for entry in trace:
        if "thinking" in entry:
            print(f"    [think]  {entry['thinking']}")
        for tc in entry.get("calls", []):
            print(f"    [call]   {tc['name']}({tc['args']})")
        if "tool_result" in entry:
            print(f"    [result] {entry['tool_result']}")
        if "answer" in entry:
            print(f"    [answer] {entry['answer']}")
        if "error" in entry:
            print(f"    [error]  {entry['error']}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tier", type=int, default=None)
    parser.add_argument("--save-results", action="store_true", help="Save test results to a file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show thinking and all tool calls per case; always saves results")
    parser.add_argument("--case-delay", type=float, default=3.0, metavar="SECONDS",
                        help="Seconds to sleep between cases (default: 3)")
    args = parser.parse_args()

    settings = load_settings()
    max_tier = args.max_tier if args.max_tier is not None else settings.max_tier
    verbose = args.verbose
    save_results = args.save_results or verbose

    registry._reset_for_tests()
    registry.load_all()
    ctx = ToolContext(settings=settings, rest=None, ws=None)  # handlers never run
    tools = build_tools(ctx, max_tier=max_tier)
    llm = build_llm(settings)
    system = timestamped_system(build_system_prompt(settings, ctx.skills_dir))
    output: dict = {
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "num_gpu": settings.num_gpu,
        "num_ctx": settings.num_ctx,
        "cases": {},
    }

    cases = yaml.safe_load(CASES_FILE.read_text())
    passed = 0
    run = 0
    subset = settings.enable_tool_subsetting
    print(f"model: {settings.llm_model} via {settings.llm_provider} "
          f"(tool_subsetting={'on' if subset else 'off'})\n")
    t0 = time.monotonic()
    for case in cases:
        print("-" * 20)
        if case.get("min_tier", 1) > max_tier:
            print(f"  SKIP  {case['id']} (needs tier {case['min_tier']})")
            continue
        run += 1
        # Mirror production: the agent only sees the per-query tool subset.
        case_tools = select_tools(tools, case["prompt"]) if subset else tools
        trace: list[dict] = []
        if verbose:
            trace = await _agent_trace(settings, case["prompt"])
            lc_calls = next(
                (entry["calls"] for entry in trace if "calls" in entry), []
            )
            ok, reason = check(case, lc_calls)
            passed += ok
            case_data: dict = {"passed": ok}
            if reason:
                case_data["reason"] = reason
            case_data['prompt'] = case["prompt"]
            case_data["trace"] = trace
        else:
            bound = llm.bind_tools(case_tools)
            msg = await bound.ainvoke([system, HumanMessage(case["prompt"])])
            ok, reason = check(case, msg.tool_calls)
            passed += ok
            case_data = {"passed": ok}
            if reason:
                case_data["reason"] = reason

        output["cases"][case["id"]] = case_data
        print(f"  {'PASS' if ok else 'FAIL'}  {case['id']}" + (f" — {reason}" if reason else ""))
        if verbose:
            _print_trace(trace)
        if args.case_delay > 0 and case is not cases[-1]:
            await asyncio.sleep(args.case_delay)

    elapsed = time.monotonic() - t0
    output["score"] = f"{passed}/{run}"
    output["elapsed"] = round(elapsed, 1)
    print(f"\nscore: {passed}/{run}")
    print(f"\n[{elapsed:.1f}s]\n")
    if save_results:
        _write_to_file(output)
    return 0 if passed == run else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
