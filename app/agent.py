import os

from langchain_ollama import ChatOllama
from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver

from .tools import READ_ONLY_TOOLS

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
)

# In-memory conversation state per thread_id. Swap MemorySaver for a
# persistent checkpointer (e.g. SqliteSaver) if you want chats to survive
# an add-on restart.
_checkpointer = MemorySaver()

agent = create_agent(
    model=llm,
    tools=READ_ONLY_TOOLS,
    system_prompt=SYSTEM_PROMPT,
    checkpointer=_checkpointer,
)


async def ask(message: str, thread_id: str = "default") -> str:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": message}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    last_message = result["messages"][-1]
    return last_message.content
