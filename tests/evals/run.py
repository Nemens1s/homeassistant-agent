"""Tool-selection eval. Plain runs grade the first tool call only. Verbose
runs (-v) run the whole agent and grade whether it reached the expected call
at any point, with no forbidden tool and a final answer; the first-call grade
is kept as `direct`. Usage:

    uv run python -m tests.evals.run            # uses .env settings
    LLM_MODEL=llama3.1:8b uv run python -m tests.evals.run
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.json import parse_partial_json

from app.agent.factory import build_agent, build_system_prompt
from app.agent.middleware.current_time_middleware import current_time_line
from app.agent.llm import build_llm
from app.agent.tool_router import select_tools
from app.config import load_settings
from app.constants import WRITE_DOMAINS
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.memory.store import NoteStore
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.context import ToolContext

CASES_FILE = Path(__file__).parent / "cases.yaml"


def _param_mismatch(case: dict, args: dict) -> str:
    """Why `args` miss the case's expect_params, or "" when they match.

    Case-insensitive for strings: HA area/friendly names vary in case and the
    handlers match case-insensitively, so "Bedroom" == "bedroom" is a pass."""
    for key, exp_val in (case.get("expect_params") or {}).items():
        actual = args.get(key)
        if isinstance(actual, str) and isinstance(exp_val, str):
            if actual.lower() != exp_val.lower():
                return f"param {key}={actual!r}, expected {exp_val!r}"
        elif actual != exp_val:
            return f"param {key}={actual!r}, expected {exp_val!r}"
    return ""


def _call_mismatch(case: dict, call: dict) -> str:
    """Why this call isn't what the case expects, or "" when it is."""
    # Accept expect_tool (str) or expect_any_tool (list) — both are valid.
    any_of = case.get("expect_any_tool")
    expected = any_of or [case.get("expect_tool")]
    if call["name"] not in expected:
        return f"called {call['name']} (args {call['args']})"
    # Skip param check when the model called an alternate tool from
    # expect_any_tool instead of the primary expect_tool.
    if any_of and call["name"] != case.get("expect_tool"):
        return ""
    return _param_mismatch(case, call["args"])


def _forbidden(case: dict) -> list[str]:
    forbidden = list(case.get("forbid_tools") or [])
    if case.get("expect_not_tool"):
        forbidden.append(case["expect_not_tool"])
    return forbidden


def check(case: dict, tool_calls: list) -> tuple[bool, str]:
    """Direct grade: was the FIRST tool call the expected one?"""
    banned = case.get("expect_not_tool")
    if banned:
        if tool_calls and tool_calls[0]["name"] == banned:
            return False, f"called banned tool {banned} (args {tool_calls[0]['args']})"
        return True, ""

    if not tool_calls:
        return False, "no tool call"
    call = tool_calls[0]
    if call["name"] in _forbidden(case):
        return False, f"called forbidden tool {call['name']}"
    mismatch = _call_mismatch(case, call)
    return mismatch == "", mismatch


def all_calls(trace: list[dict]) -> list[dict]:
    """Every tool call in the trace, in order."""
    calls = []
    for entry in trace:
        for call in entry.get("calls", []):
            calls.append(call)
    return calls


def check_reached(case: dict, trace: list[dict]) -> tuple[bool, str]:
    """Reached grade (verbose runs): the expected call happened at any point,
    no forbidden tool was called, and the run ended with an answer. Looking
    something up first is fine; acting wrongly or never answering is not."""
    for entry in trace:
        if entry.get("error"):
            return False, f"agent error: {entry['error']}"

    calls = all_calls(trace)
    forbidden = _forbidden(case)
    for call in calls:
        if call["name"] in forbidden:
            return False, f"called forbidden tool {call['name']}"

    if not case.get("expect_not_tool"):
        first_mismatch = ""
        found = False
        for call in calls:
            mismatch = _call_mismatch(case, call)
            if mismatch == "":
                found = True
                break
            # Remember a wrong-params miss on the right tool: it explains more
            # than "never called".
            if not first_mismatch and mismatch.startswith("param "):
                first_mismatch = mismatch
        if not found:
            if first_mismatch:
                return False, first_mismatch
            return False, f"never called {case.get('expect_tool')}"

    if not trace or not trace[-1].get("answer"):
        return False, "no final answer"
    return True, ""


EXPECT_KEYS = ("expect_tool", "expect_any_tool", "expect_not_tool", "expect_params", "forbid_tools")


def case_expectation(case: dict) -> dict:
    """What the case checked, copied from cases.yaml so an old results file
    can be read without today's (possibly edited) case list."""
    expected = {}
    for key in EXPECT_KEYS:
        if key in case:
            expected[key] = case[key]
    return expected


def case_record(case: dict, ok: bool, reason: str, duration_s: float,
                tool_errors: list[str] | None = None,
                trace: list[dict] | None = None,
                direct: tuple[bool, str] | None = None) -> dict:
    """One case's entry in the results file. In verbose runs `ok`/`reason` are
    the reached grade and `direct` is the first-call grade."""
    record: dict = {"passed": ok}
    if direct is not None:
        record["direct"] = direct[0]
        if direct[1]:
            record["direct_reason"] = direct[1]
    if reason:
        record["reason"] = reason
    if tool_errors:
        record["tool_errors"] = tool_errors
    record["prompt"] = case["prompt"]
    record["expected"] = case_expectation(case)
    record["duration_s"] = round(duration_s, 1)
    if trace is not None:
        record["trace"] = trace
    return record


def _write_to_file(output: dict) -> None:
    title = f"{datetime.now().isoformat(timespec='seconds')} {output['model']} {output['provider']}"
    out_dir = Path(__file__).parent.parent.parent / ".evals"
    out_dir.mkdir(exist_ok=True)
    filename = title.replace(":", "-").replace(" ", "_").replace("/", "-") + ".json"
    (out_dir / filename).write_text(json.dumps(output, indent=2))

async def _open_context(settings) -> ToolContext:
    """Build the same ToolContext production builds (app/main.py), so the
    availability probes and the agent run see real HA. The note store is
    in-memory: notes the evals create are thrown away at exit and can never
    reach the add-on's listener."""
    write_domains = WRITE_DOMAINS if settings.allow_actions else ()
    rest = RestClient(settings.ha_base_url, settings.ha_token, allowed_write_domains=write_domains)
    ws: WebSocketClient | None = None
    try:
        _ws = WebSocketClient(settings.ws_url, settings.ha_token)
        await _ws.start(connect_timeout=settings.ws_connect_timeout)
        ws = _ws
    except Exception:
        print("warning: websocket unavailable; ws-backed tools will report ws_unavailable")
    note_store = NoteStore("")
    notes = note_store if note_store.available else None
    return ToolContext(settings=settings, rest=rest, ws=ws, notes=notes)


async def _close_context(ctx: ToolContext) -> None:
    await ctx.rest.aclose()
    if ctx.ws:
        await ctx.ws.stop()
    if ctx.notes:
        ctx.notes.close()


def _tool_errors(trace: list[dict]) -> list[str]:
    """Error codes of every tool result in the trace that came back status=error."""
    codes = []
    for entry in trace:
        raw = entry.get("tool_result")
        if raw is None:
            continue
        try:
            result = json.loads(raw)
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("status") == "error":
            error = result.get("error") or {}
            codes.append(error.get("code") or "unknown")
    return codes


async def _agent_trace(settings, ctx: ToolContext, enabled, prompt: str) -> list[dict]:
    """Run the full agent against real HA and capture every turn as a list of dicts.
    Each entry is one of:
      {"thinking": ..., "calls": [...]}   — AI turn with tool proposals
      {"tool_result": "..."}              — tool response
      {"thinking": ..., "answer": "..."}  — final AI turn with text reply
      {"error": "..."}                    — agent raised an exception
    """
    # A fresh agent per case so the loop guard starts clean.
    agent = build_agent(settings, ctx, enabled=enabled)
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
                # OpenAI-compatible providers (llama.cpp, etc.) stream args incrementally — the first
                # delta is often a lone "{". json.loads on that partial raises; use
                # LangChain's streaming-tolerant parser (same one it uses internally).
                current["calls"] = [
                    {"name": v["name"], "args": parse_partial_json(v["args"]) if v["args"] else {}}
                    for v in pending.values() if v["name"]
                ]

            if txt := (token.content if isinstance(token.content, str) else ""):
                current["answer"] = current.get("answer", "") + txt

        if current:
            trace.append({k: v for k, v in current.items() if v})
    except Exception as exc:
        trace.append({"error": str(exc)})

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
    # Sampling overrides — when given, they replace the configured values for this
    # run and are recorded in the results file. min_p/top_k/repeat_penalty only
    # reach the model on the llamacpp provider (see app/agent/llm.py).
    parser.add_argument("--temp", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--min-p", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--repeat-penalty", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    settings = load_settings()

    # Apply any sampling overrides from the CLI, and note them for the output file.
    sampling_overrides: dict = {}
    if args.temp is not None:
        sampling_overrides["temperature"] = args.temp
    if args.top_p is not None:
        sampling_overrides["top_p"] = args.top_p
    if args.min_p is not None:
        sampling_overrides["min_p"] = args.min_p
    if args.top_k is not None:
        sampling_overrides["top_k"] = args.top_k
    if args.repeat_penalty is not None:
        sampling_overrides["repeat_penalty"] = args.repeat_penalty
    if args.seed is not None:
        sampling_overrides["seed"] = args.seed
    if sampling_overrides:
        settings = settings.model_copy(update=sampling_overrides)

    max_tier = args.max_tier if args.max_tier is not None else settings.max_tier
    verbose = args.verbose
    save_results = args.save_results or verbose

    registry._reset_for_tests()
    registry.load_all()
    ctx = await _open_context(settings)
    try:
        return await _run_cases(settings, ctx, max_tier, verbose, save_results,
                                args.case_delay, sampling_overrides)
    finally:
        await _close_context(ctx)


async def _run_cases(settings, ctx, max_tier, verbose, save_results,
                     case_delay, sampling_overrides) -> int:
    # Same probes as production: tools with nothing in this home to act on
    # are left off the menu.
    enabled = await registry.resolve_enabled(ctx)
    tools = build_tools(ctx, max_tier=max_tier, enabled=enabled)
    llm = build_llm(settings)
    system = SystemMessage(build_system_prompt(settings, ctx.skills_dir))
    output: dict = {
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "num_ctx": settings.num_ctx,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        # verbose runs grade the whole run (reached) and keep the first-call
        # grade as `direct`; plain runs only see the first call.
        "grading": "reached" if verbose else "first_call",
        "cases": {},
    }
    if sampling_overrides:
        output["sampling_overrides"] = sampling_overrides

    cases = yaml.safe_load(CASES_FILE.read_text())
    passed = 0
    passed_with_errors = 0
    direct_passed = 0
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
        if case.get("requires_skills") and not settings.skills_enabled:
            print(f"  SKIP  {case['id']} (needs skills_enabled)")
            continue
        run += 1
        # Mirror production: the agent only sees the per-query tool subset.
        case_tools = select_tools(tools, case["prompt"]) if subset else tools
        trace: list[dict] = []
        tool_errors: list[str] = []
        if verbose:
            start = time.monotonic()
            trace = await _agent_trace(settings, ctx, enabled, case["prompt"])
            duration = time.monotonic() - start
            lc_calls = next(
                (entry["calls"] for entry in trace if "calls" in entry), []
            )
            direct = check(case, lc_calls)
            ok, reason = check_reached(case, trace)
            tool_errors = _tool_errors(trace)
            passed += ok
            direct_passed += direct[0]
            # A pass whose run hit tool errors on the way is flagged so it
            # doesn't hide behind the score.
            if tool_errors and ok:
                passed_with_errors += 1
            case_data = case_record(case, ok, reason, duration,
                                    tool_errors=tool_errors, trace=trace, direct=direct)
        else:
            bound = llm.bind_tools(case_tools)
            start = time.monotonic()
            msg = await bound.ainvoke([system, HumanMessage(case["prompt"] + "\n\n" + current_time_line())])
            duration = time.monotonic() - start
            ok, reason = check(case, msg.tool_calls)
            passed += ok
            case_data = case_record(case, ok, reason, duration)

        output["cases"][case["id"]] = case_data
        status = "PASS" if ok else "FAIL"
        if ok and tool_errors:
            status = "PASS*"
        line = f"  {status}  {case['id']}"
        if reason:
            line += f" — {reason}"
        if verbose and ok and not case_data["direct"]:
            line += f" (not direct: {case_data['direct_reason']})"
        if tool_errors:
            line += f" (tool errors: {', '.join(tool_errors)})"
        print(line)
        if verbose:
            _print_trace(trace)
        if case_delay > 0 and case is not cases[-1]:
            await asyncio.sleep(case_delay)

    elapsed = time.monotonic() - t0
    output["score"] = f"{passed}/{run}"
    if verbose:
        output["direct_score"] = f"{direct_passed}/{run}"
        output["passed_with_tool_errors"] = passed_with_errors
    output["elapsed"] = round(elapsed, 1)
    print(f"\nscore: {passed}/{run}")
    if verbose:
        print(f"  direct (right tool first): {direct_passed}/{run}")
    if verbose and passed_with_errors:
        print(f"  of which {passed_with_errors} passed with tool errors (PASS*)")
    print(f"\n[{elapsed:.1f}s]\n")
    if save_results:
        _write_to_file(output)
    return 0 if passed == run else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
