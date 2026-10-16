"""Typed settings — the single place configuration is read.

In the add-on container, options come from /data/options.json (written by the
Supervisor from the add-on's Configuration tab) plus SUPERVISOR_TOKEN from the
environment. In dev, everything comes from .env / environment variables.
"""

import json
import logging
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OPTIONS_FILE = Path("/data/options.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Home Assistant
    ha_base_url: str = "http://supervisor/core"
    ha_token: str = Field(
        default="", validation_alias=AliasChoices("SUPERVISOR_TOKEN", "HA_TOKEN")
    )
    ws_connect_timeout: float = 10.0

    # LLM backend
    # "auto" (default) probes llm_url to pick ollama/llamacpp/openai; see
    # app/agent/llm_probe.py. Set explicitly to skip the probe.
    llm_provider: str = "auto"  # "ollama", "llamacpp" (llama-server), or anything else = OpenAI-compatible
    llm_url: str = Field(
        "http://localhost:11434",
        validation_alias=AliasChoices("LLM_URL"),
    )
    llm_model: str = "hf.co/empero-ai/Qwen3.8-2B-GGUF:Q6_K"
    api_key: str = ""  # key for an OpenAI-compatible endpoint; unused by Ollama

    # Model options — small-model tuning (spec: LLM factory & model settings)
    temperature: float = Field(
        0.0, description="Sampling randomness; 0 = greedy/deterministic, higher = more varied."
    )
    top_p: float = Field(
        0.95, description="Nucleus sampling: keep the smallest token set with cumulative prob >= this (1.0 = off)."
    )
    min_p: float = Field(
        0.05, description="Drop tokens below this fraction of the top token's probability (0 = off). llamacpp path."
    )
    top_k: int = Field(
        40, description="Sample only from the K most likely tokens (0 = off). llamacpp path."
    )
    repeat_penalty: float = Field(
        1.1, description="Penalty on already-seen tokens to curb repetition (1.0 = none). llamacpp path."
    )
    seed: int = 42
    reasoning: bool = False
    num_predict: int = 2048
    num_gpu: int = 16
    num_ctx: int = 8192
    keep_alive: int = -1

    show_thinking: bool = False  # stream <think> content to stdout in the CLI

    # Agent behavior
    # Appended after the code-owned base prompt (app/agent/factory.py
    # build_system_prompt) under a "HOUSEHOLD INSTRUCTIONS" heading. Empty =
    # nothing added.
    extra_instructions: str = ""
    # off = read-only; on = trigger_action/notify_user/scheduling are available.
    allow_actions: bool = False
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
    max_history_messages: int = 40
    enable_tool_subsetting: bool = True  # narrow the tool menu per query (small-model aid)
    # Tool names to never offer the agent, regardless of tier or probe result.
    # Empty = nothing switched off beyond tier gating and availability probes.
    disabled_tools: list[str] = []
    # Pick which vacuum.* entity get_vacuum_state uses when there is more than
    # one. Empty = the first vacuum.* sorted by entity_id. Hidden option (not
    # in config.yaml's options:), set via the add-on's Advanced options.
    vacuum_entity: str = ""

    # Fast path (optional): an external service that maps a request straight to
    # one ai_* action, skipping the LLM. Contract: README "Fast path". Off by
    # default: when enabled AND allow_actions is on, the service may trigger an
    # ai_* automation directly, bypassing the agent. See docs spec 2026-08-25.
    fast_path_enabled: bool = False
    fast_path_url: str = ""
    # Fire only at or above this confidence. Keep 0 unless your model reports a
    # calibrated confidence (e.g. a model fine-tuned on Needle's site).
    fast_path_confidence_threshold: float = 0.0

    # Language layer. Off by default: when
    # disabled the HTTP edge behaves exactly as before and lang-mt is never
    # called. Translation happens only here, never inside the agent graph.
    language_layer_enabled: bool = False
    lang_mt_url: str = "http://localhost:10100"
    languages: list[str] = ["en", "ru", "et"]
    default_language: str = "en"
    min_confidence: float = 0.5   # below this, fall back to the thread prior
    lang_mt_timeout_s: float = 3.0
    glossary_path: str = "/config/gosling/glossary.yaml"

    # Telemetry (local SQLite). Set telemetry_enabled=False to opt out.
    # telemetry_db_path: local SQLite file; empty = telemetry disabled.
    telemetry_enabled: bool = True
    telemetry_db_path: str = "/data/telemetry.sqlite"
    # Daily pruning (app/retention.py); 0 keeps everything. Labelled
    # telemetry requests are always kept.
    telemetry_retention_days: int = Field(30, ge=0)
    thread_retention_days: int = Field(30, ge=0)

    # Event-triggered memory notes (spec 2026-10-04). The agent saves one-shot
    # notes; the harness fires them when a watched entity changes state or the
    # clock reaches a time. Empty watched_entities = state notes off; empty
    # clock_entity = time notes off.
    watched_entities: list[str] = []
    clock_entity: str = ""  # World Clock entity, e.g. sensor.europe_tallinn, "HH:MM DD-MM-YYYY"
    # Off = the agent still calls notify_user after acting, but nothing is sent
    # (reminders the user asked for are always sent).
    event_confirmations_enabled: bool = True
    memory_note_default_ttl_hours: int = Field(24, ge=1)
    memory_note_max_ttl_hours: int = Field(168, ge=1)
    # A time note this late expires instead of firing. 0 would expire every
    # note on its own fire tick, hence ge=1.
    time_note_grace_minutes: int = Field(120, ge=1)
    notify_action: str = "script.ai_action_notify"  # the only script notify_user calls

    # Skills: user-supplied markdown playbooks dropped into skills_path (see
    # docs/examples/skills/ for the format). Off by default — no skills ship
    # in the image. load_skill is only offered when this is on and at least
    # one skill file parses.
    skills_enabled: bool = False
    skills_path: str = "/config/gosling/skills"

    @field_validator("notify_action")
    @classmethod
    def _normalise_notify_action(cls, value: str) -> str:
        # Compared by exact string to keep the notify script off trigger_action
        # and the fast path; "Script.AI_..." must not slip past that.
        return value.strip().lower()

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
    def max_tier(self) -> int:
        # Tier is an internal concept (tools/base.Tier); users only see
        # allow_actions. Kept so every settings.max_tier call site keeps
        # working unchanged.
        if self.allow_actions:
            return 2
        return 1

    @property
    def ws_url(self) -> str:
        base = self.ha_base_url.replace("http://", "ws://").replace("https://", "wss://")
        if base.endswith("/core"):
            return f"{base}/websocket"  # supervisor proxy path
        return f"{base}/api/websocket"  # direct HA instance


def warn_if_llm_unset(settings: Settings) -> None:
    """Log one warning when llm_url is empty — e.g. after an upgrade where the
    Configuration tab was never saved. Startup carries on regardless."""
    if not settings.llm_url.strip():
        logging.getLogger("config").warning(
            "llm_url is not set — set it in the add-on Configuration tab"
        )


def load_settings() -> Settings:
    if OPTIONS_FILE.exists():
        data = json.loads(OPTIONS_FILE.read_text())
        filtered = {}
        for k, v in data.items():
            if v is not None:
                filtered[k] = v
        return Settings(**filtered)
    return Settings()
