"""Typed settings — the single place configuration is read.

In the add-on container, options come from /data/options.json (written by the
Supervisor from the add-on's Configuration tab) plus SUPERVISOR_TOKEN from the
environment. In dev, everything comes from .env / environment variables.
"""

import json
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

OPTIONS_FILE = Path("/data/options.json")

DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant for this Home Assistant instance. "
    "Answer questions about the home using the available tools. "
    "Always look up real data with tools instead of guessing. "
    "Once you got an answer from tool, do not overthink, present information as you found it"
    "Follow instructions precisely, do not do anything extra that wasn't asked"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Home Assistant
    ha_base_url: str = "http://supervisor/core"
    ha_token: str = Field(
        default="", validation_alias=AliasChoices("SUPERVISOR_TOKEN", "HA_TOKEN")
    )
    ws_connect_timeout: float = 10.0

    # LLM backend
    llm_provider: str = "ollama"  # "ollama", "llamacpp" (llama-server HTTP API), or any litellm provider
    llm_url: str = Field(
        "http://localhost:11434",
        validation_alias=AliasChoices("LLM_URL", "OLLAMA_URL"),
    )
    llm_model: str = "hf.co/empero-ai/Qwen3.8-2B-GGUF:Q6_K"
    api_key: str = ""  # cloud provider key, used only by litellm providers

    # Model options — small-model tuning (spec: LLM factory & model settings)
    temperature: float = 0.0
    seed: int = 42
    reasoning: bool = False
    num_predict: int = 2048
    num_gpu: int = 16
    num_ctx: int = 8192
    keep_alive: int = -1

    show_thinking: bool = False  # stream <think> content to stdout in the CLI

    # Agent behavior
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_tier: int = 1
    allowed_domains: list[str] = ["light", "switch", "fan", "automation"]
    ai_actions_switch: str = "input_boolean.ai_triggered_actions"  # master gate; "" disables
    recursion_limit: int = 15
    max_rows: int = 50
    audit_db_path: str = ""
    # Durable conversation memory. Empty = in-memory (lost on restart); set a
    # path (e.g. /data/checkpoints.sqlite in the addon) to persist threads.
    checkpoint_db_path: str = ""
    # Per-thread history cap: after each run, prune stored history to the last N
    # messages (rounded to a human-turn boundary). 0 = keep full history. This
    # bounds unbounded per-thread growth, especially with a durable checkpointer.
    max_history_messages: int = 0
    enable_tool_subsetting: bool = True  # narrow the tool menu per query (small-model aid)

    # Needle fast path (optional low-latency automation triggering). Off by
    # default: when enabled AND max_tier >= 2, the needle service may trigger an
    # ai_* automation directly, bypassing the agent. See docs spec 2026-08-25.
    needle_enabled: bool = False
    needle_remote_url: str = ""          # URL of the needle inference service
    # Fire only when confidence >= this. Default 0.0 = "trust the call" (fire on
    # any emitted tool call): fine-tuned Needle weights report confidence as None
    # (the head is not tuned), so a nonzero threshold would block EVERYTHING with
    # a tuned model. Raise it only if running the calibrated BASE model.
    needle_confidence_threshold: float = 0.0
    needle_menu_ttl_s: int = 60          # menu/grammar cache TTL

    # Person display names: list of {ha_name: "<HA friendly_name>", name: "<shown name>"}
    # ha_name is the lookup key (what HA reports); name is what the agent sees.
    person_name_map: list[dict[str, str]] = []
    # Person exclusions: person entities whose friendly_name contains any of these
    # substrings (case-insensitive) are hidden from get_person_locations output.
    person_name_exclude: list[str] = []

    @property
    def person_name_lookup(self) -> dict[str, str]:
        lookup = {}
        for e in self.person_name_map:
            lookup[e["ha_name"]] = e["name"]
        return lookup

    @property
    def ws_url(self) -> str:
        base = self.ha_base_url.replace("http://", "ws://").replace("https://", "wss://")
        if base.endswith("/core"):
            return f"{base}/websocket"  # supervisor proxy path
        return f"{base}/api/websocket"  # direct HA instance


def load_settings() -> Settings:
    if OPTIONS_FILE.exists():
        data = json.loads(OPTIONS_FILE.read_text())
        filtered = {}
        for k, v in data.items():
            if v is not None:
                filtered[k] = v
        return Settings(**filtered)
    return Settings()
