"""Capability-testing REPL. Streams tokens; tool calls appear via the
agent.tools audit log lines (INFO). Requires HA_BASE_URL, HA_TOKEN and an
Ollama server (see .env)."""

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import IO

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError

from app.agent.checkpointer import open_checkpointer
from app.agent.factory import build_agent
from app.audit import AuditSink
from app.needle.factory import build_fast_path_router
from app.config import load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def _content_str(content, max_len: int = 300) -> str:
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", ""))
            else:
                parts.append(str(b))
        content = " ".join(parts)
    content = str(content)
    return content[:max_len] + "…" if len(content) > max_len else content


def _print_history(messages: list) -> None:
    if not messages:
        print("  (no history)")
        return
    for msg in messages:
        role = getattr(msg, "type", "?")
        if isinstance(msg, AIMessage) and msg.tool_calls:
            call_parts = []
            for tc in msg.tool_calls:
                call_parts.append(f"{tc['name']}({tc['args']})")
            calls = ", ".join(call_parts)
            print(f"  [{role}] → tool calls: {calls}")
        elif isinstance(msg, ToolMessage):
            print(f"  [tool/{msg.name}] {_content_str(msg.content)}")
        else:
            print(f"  [{role}] {_content_str(msg.content)}")


def _open_conv_file(base_dir: Path) -> IO[str]:
    base_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y-%m-%dT%H-%M-%S.txt")
    return open(base_dir / name, "w", encoding="utf-8")


async def main(save_conversations: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    settings = load_settings()

    write_domains = (
        tuple(settings.allowed_domains) if settings.max_tier >= 2 else ()
    )
    rest = RestClient(settings.ha_base_url, settings.ha_token,
                      allowed_write_domains=write_domains)
    audit = AuditSink(settings.audit_db_path)
    ws: WebSocketClient | None = WebSocketClient(settings.ws_url, settings.ha_token)
    try:
        await ws.start(connect_timeout=settings.ws_connect_timeout)
    except Exception as exc:
        print(f"warning: websocket unavailable ({exc}) — area/automation tools degraded")
        ws = None

    ctx = ToolContext(settings=settings, rest=rest, ws=ws, audit=audit)
    checkpointer_cm = open_checkpointer(settings)
    checkpointer = await checkpointer_cm.__aenter__()
    agent = build_agent(settings, ctx, checkpointer=checkpointer)
    fast_path = None
    try:
        fast_path = build_fast_path_router(settings, rest, ctx)
    except Exception as exc:
        print(f"warning: needle fast path unavailable ({exc})")
    config = {
        "configurable": {"thread_id": "cli"},
        "recursion_limit": settings.recursion_limit,
    }

    conv_file: IO[str] | None = (
        _open_conv_file(Path(".conversations")) if save_conversations else None
    )
    needle_note = "  [needle fast path ON]" if fast_path is not None else ""
    print(f"\nAgent ready ({settings.llm_model} via {settings.llm_provider}).{needle_note} Ctrl+C to quit.\n")
    try:
        while True:
            try:
                user_input = input("You: ").strip()
                if user_input == "bye":
                    print("Shutting down")
                    break
            except (KeyboardInterrupt, EOFError):
                print("\nBye.")
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                cmd = user_input.lower()
                if cmd == "/history":
                    state = await agent.aget_state(config)
                    msgs = state.values.get("messages", [])
                    print(f"  --- {len(msgs)} message(s) in history ---")
                    _print_history(msgs)
                    print()
                elif cmd == "/clear":
                    await agent.aupdate_state(config, {"messages": []})
                    print("  (history cleared)\n")
                else:
                    print(f"  Unknown command: {user_input}  (available: /history, /clear)\n")
                continue
            if conv_file:
                conv_file.write(f"{_ts()} You: {user_input}\n")
                conv_file.flush()
            # Needle fast path: if it handles the turn (emits a trigger), skip the agent.
            if fast_path is not None:
                fp_reply = await fast_path.try_fast_path(user_input, "cli")
                if fp_reply is not None:
                    print(f"Agent (fast path): {fp_reply}")
                    if conv_file:
                        conv_file.write(f"{_ts()} Agent (fast path): {fp_reply}\n")
                        conv_file.flush()
                    continue
                # print("Stopping here for test")
                # continue
            print("Agent: ", end="", flush=True)
            t0 = time.monotonic()
            try:
                content_buf: list[str] = []
                think_buf: list[str] = []
                pending_tool_calls: dict[int, dict] = {}  # index → {name, args, id}
                has_tool_calls = False
                last_response_meta: dict = {}
                async for token, _meta in agent.astream(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config=config,
                    stream_mode="messages",
                ):
                    if not isinstance(token, AIMessageChunk):
                        visible = content_buf or (think_buf if not has_tool_calls else [])
                        if visible and not has_tool_calls:
                            print("".join(visible), end="", flush=True)
                        if conv_file and think_buf:
                            conv_file.write(f"{_ts()} <think> {''.join(think_buf)} </think>\n")
                            conv_file.flush()
                        content_buf = []
                        think_buf = []
                        has_tool_calls = False
                        if conv_file and isinstance(token, ToolMessage):
                            raw = token.content
                            result = raw if isinstance(raw, str) else json.dumps(raw)
                            for call in pending_tool_calls.values():
                                if call.get("id") == token.tool_call_id:
                                    conv_file.write(
                                        f"{_ts()} Tool: {call['name']}({call['args']}) → {result}\n"
                                    )
                                    conv_file.flush()
                                    break
                        continue
                    if token.response_metadata:
                        last_response_meta = token.response_metadata
                    thinking = token.additional_kwargs.get("reasoning_content", "")
                    if thinking:
                        think_buf.append(thinking)
                        if settings.show_thinking:
                            print(f"\033[2m{thinking}\033[0m", end="", flush=True)
                    if token.tool_call_chunks:
                        has_tool_calls = True
                        if conv_file:
                            for chunk in token.tool_call_chunks:
                                idx = chunk.get("index") or 0
                                if idx not in pending_tool_calls:
                                    pending_tool_calls[idx] = {"name": "", "args": "", "id": ""}
                                if chunk.get("name"):
                                    pending_tool_calls[idx]["name"] = chunk["name"]
                                if chunk.get("id"):
                                    pending_tool_calls[idx]["id"] = chunk["id"]
                                pending_tool_calls[idx]["args"] += chunk.get("args") or ""
                    raw = token.content
                    if isinstance(raw, str):
                        text = raw
                    elif isinstance(raw, list):
                        parts = []
                        for b in raw:
                            if isinstance(b, dict):
                                parts.append(b.get("text", ""))
                            else:
                                parts.append(str(b))
                        text = "".join(parts)
                    else:
                        text = ""
                    if text:
                        content_buf.append(text)
                # Flush the final turn (no non-AIMessageChunk follows it).
                visible = content_buf or (think_buf if not has_tool_calls else [])
                if visible and not has_tool_calls:
                    print("".join(visible), end="", flush=True)
                    if conv_file:
                        if think_buf and content_buf:  # thinking is separate from answer
                            conv_file.write(f"{_ts()} <think> {''.join(think_buf)} </think>\n")
                        conv_file.write(f"{_ts()} Agent: {''.join(visible)}\n")
                        conv_file.flush()
            except GraphRecursionError:
                msg = f"[stopped: hit the {settings.recursion_limit}-step limit without finishing]"
                print(f"\n{msg}", end="")
                if conv_file:
                    conv_file.write(f"{_ts()} {msg}\n")
                    conv_file.flush()
            except Exception as exc:
                print(f"\n[error: {exc}]", end="")
                if conv_file:
                    conv_file.write(f"{_ts()} [error: {exc}]\n")
                    conv_file.flush()
            elapsed = time.monotonic() - t0
            prompt_tok = last_response_meta.get("prompt_eval_count")
            gen_tok = last_response_meta.get("eval_count")
            tok_str = f" | {prompt_tok} prompt + {gen_tok} gen tokens" if prompt_tok is not None else ""
            print(f"\n[{elapsed:.1f}s{tok_str}]\n")
    finally:
        print("Closing rest client")
        await rest.aclose()
        if ws is not None:
            print("Closing ws client")
            await ws.stop()
        audit.close()
        await checkpointer_cm.__aexit__(None, None, None)
        if conv_file is not None:
            conv_file.close()


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="HA agent CLI")
    _parser.add_argument(
        "--save-conversations",
        action="store_true",
        help="Write a transcript of this session to .conversations/",
    )
    _args = _parser.parse_args()
    asyncio.run(main(save_conversations=_args.save_conversations))
