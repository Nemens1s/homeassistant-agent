# Local HA Agent (Ollama) — scaffold

Read-only chat agent for Home Assistant, backed by a local Ollama server on
a separate machine on your LAN. Runs as a HA add-on with an ingress-based
chat UI.

## Architecture

```
HA Supervisor host (add-on container)
  ├─ FastAPI app (app/main.py) — serves chat UI + /api/chat
  ├─ LangGraph ReAct agent (app/agent.py) — ChatOllama + read-only tools
  ├─ Read-only HA REST client (app/ha_client.py) — GET-only, via
  │    http://supervisor/core (SUPERVISOR_TOKEN, auto-injected)
  └─ Ollama (external) — http://<laptop-ip>:11434, set via add-on options
```

There is no write/service-call tool anywhere in this codebase. That's the
actual read-only guarantee — not a permissions toggle, but the simple fact
that no such tool exists for the model to invoke.

## Installing as a local add-on

1. Copy this folder to `/addons/local/local_ha_agent` on your HA host
   (via Samba, SSH, or the Studio Code Server add-on).
2. In HA: Settings → Add-ons → Add-on Store → ⋮ → Check for updates →
   your local add-on should appear under "Local add-ons".
3. Install it, then go to its Configuration tab and set:
   - `ollama_url`: `http://<laptop-ip>:11434`
   - `ollama_model`: a model you've pulled on the laptop, e.g. `qwen2.5:7b`
   - `system_prompt`: tweak to taste
4. Start it, then open it — ingress gives you the chat UI directly in the
   HA sidebar/panel.

## Before you rely on it: verify tool-calling actually works

Not every Ollama model reliably calls tools — some just answer in prose and
silently skip the tool call. Test explicitly with a question that requires
a tool (e.g. "what's the state of light.kitchen") and check the add-on logs
to confirm a tool call actually happened. Qwen2.5/Qwen3 and Llama 3.1+ are
solid starting points; older models (llama2, plain command-r) are less
reliable here.

## Known gaps / next steps

- **No persistent memory across restarts.** `MemorySaver` (in `agent.py`)
  is in-memory only. Swap for a `SqliteSaver` checkpoint if you want chat
  history to survive an add-on restart.
- **No auth on the FastAPI app itself.** Ingress handles access control at
  the HA level; if you ever expose this port outside ingress, add auth.
- **Tool set is intentionally minimal.** Good candidates to add next:
  weather, calendar (`/api/calendars`), a `get_area_summary` tool that
  groups entity states by area. Keep write-capable tools in a fork/branch,
  never mixed into `READ_ONLY_TOOLS`.
- **Error handling is bare.** `ha_client.py` will raise on non-2xx
  responses; you'll want to catch these in `tools.py` and return a
  friendly string instead of letting the exception bubble into the chat.
- **Long HA installs**: `list_entities` with no domain filter can return a
  lot of text and blow past a small model's context window. Consider a
  default cap or area-based filtering.
