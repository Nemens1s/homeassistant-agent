import os
import sys
from pathlib import Path

# Allow running as a script: make the project root importable so that
# `app` is a proper package and relative imports inside it resolve.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from app.tools import READ_ONLY_TOOLS

load_dotenv()

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a read-only assistant for this Home Assistant instance. "
    "You can only look up state, history, and logbook entries — you have no "
    "ability to control any devices, even if asked. Be concise and factual.",
)


llm = ChatOllama(
    base_url=OLLAMA_URL,
    model=OLLAMA_MODEL,
    temperature=0,
    reasoning=False
)

_checkpointer = MemorySaver()

agent = create_agent(
    model=llm,
    tools=READ_ONLY_TOOLS,
    system_prompt=SYSTEM_PROMPT,
    checkpointer=_checkpointer,
)


async def _chat_loop() -> None:
    config = {"configurable": {"thread_id": "local-test"}}
    print(f"Agent ready ({OLLAMA_MODEL} @ {OLLAMA_URL}). Ctrl+C to quit.\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break
        if not user_input:
            continue
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
        )
        reply = result["messages"][-1].content
        print(f"Agent: {reply}\n")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_chat_loop())

