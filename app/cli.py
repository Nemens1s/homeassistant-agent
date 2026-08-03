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

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError
from litellm.proxy.guardrails.guardrail_hooks.custom_code.primitives import lower

from app.agent.factory import build_agent
from app.audit import AuditSink
from app.config import load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


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
    agent = build_agent(settings, ctx)
    config = {
        "configurable": {"thread_id": "cli"},
        "recursion_limit": settings.recursion_limit,
    }

    conv_file: IO[str] | None = (
        _open_conv_file(Path(".conversations")) if save_conversations else None
    )
    print(f"\nAgent ready ({settings.llm_model} via {settings.llm_provider}). Ctrl+C to quit.\n")
    try:
        while True:
            try:
                user_input = input("You: ").strip()
                if user_input == lower("Bye"):
                    print("Shutting down")
                    break
            except (KeyboardInterrupt, EOFError):
                print("\nBye.")
                break
            if not user_input:
                continue
            if conv_file:
                conv_file.write(f"{_ts()} You: {user_input}\n")
                conv_file.flush()
            print("Agent: ", end="", flush=True)
            t0 = time.monotonic()
            try:
                content_buf: list[str] = []
                think_buf: list[str] = []
                pending_tool_calls: dict[int, dict] = {}  # index → {name, args, id}
                has_tool_calls = False
                async for token, _meta in agent.astream(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config=config,
                    stream_mode="messages",
                ):
                    if not isinstance(token, AIMessageChunk):
                        if content_buf and not has_tool_calls:
                            print("".join(content_buf), end="", flush=True)
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
                    if settings.show_thinking:
                        thinking = token.additional_kwargs.get("reasoning_content", "")
                        if thinking:
                            print(f"\033[2m{thinking}\033[0m", end="", flush=True)
                            if conv_file:
                                think_buf.append(thinking)
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
                    if isinstance(token.content, str) and token.content:
                        content_buf.append(token.content)
                # Flush the final turn (no non-AIMessageChunk follows it).
                if content_buf and not has_tool_calls:
                    print("".join(content_buf), end="", flush=True)
                    if conv_file:
                        if think_buf:
                            conv_file.write(f"{_ts()} <think> {''.join(think_buf)} </think>\n")
                        conv_file.write(f"{_ts()} Agent: {''.join(content_buf)}\n")
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
            print(f"\n[{elapsed:.1f}s]\n")
    finally:
        print("Closing rest client")
        await rest.aclose()
        if ws is not None:
            print("Closing ws client")
            await ws.stop()
        audit.close()
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
