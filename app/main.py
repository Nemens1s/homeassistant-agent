from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import ask

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"


class ChatResponse(BaseModel):
    reply: str


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    reply = await ask(req.message, req.thread_id)
    return ChatResponse(reply=reply)


# Mounted last so /api/chat above takes precedence over the catch-all.
# Frontend must use *relative* fetch paths (e.g. "api/chat", not "/api/chat")
# since HA ingress serves this under a per-add-on path prefix.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
