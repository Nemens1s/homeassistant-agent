"""Capability-testing REPL. Streams tokens; tool calls appear via the
agent.tools audit log lines (INFO). Requires HA_BASE_URL, HA_TOKEN and an
Ollama server (see .env)."""

import asyncio
import logging
import time

from langchain_core.messages import AIMessageChunk
from langgraph.errors import GraphRecursionError
from litellm.proxy.guardrails.guardrail_hooks.custom_code.primitives import lower

from app.agent.factory import build_agent
from app.audit import AuditSink
from app.config import load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext


async def main() -> None:
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
            print("Agent: ", end="", flush=True)
            t0 = time.monotonic()
            try:
                async for token, _meta in agent.astream(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config=config,
                    stream_mode="messages",
                ):
                    if not isinstance(token, AIMessageChunk):
                        continue
                    if settings.show_thinking:
                        thinking = token.additional_kwargs.get("reasoning_content", "")
                        if thinking:
                            print(f"\033[2m{thinking}\033[0m", end="", flush=True)
                    if isinstance(token.content, str):
                        print(token.content, end="", flush=True)
            except GraphRecursionError:
                print(f"\n[stopped: hit the {settings.recursion_limit}-step limit without finishing]", end="")
            except Exception as exc:
                print(f"\n[error: {exc}]", end="")
            elapsed = time.monotonic() - t0
            print(f"\n[{elapsed:.1f}s]\n")
    finally:
        print("Closing rest client")
        await rest.aclose()
        if ws is not None:
            print("Closing ws client")
            await ws.stop()
        audit.close()


if __name__ == "__main__":
    asyncio.run(main())
