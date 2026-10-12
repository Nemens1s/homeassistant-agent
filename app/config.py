"""Typed settings — the single place configuration is read.

In the add-on container, options come from /data/options.json (written by the
Supervisor from the add-on's Configuration tab) plus SUPERVISOR_TOKEN from the
environment. In dev, everything comes from .env / environment variables.
"""

import json
import logging
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OPTIONS_FILE = Path("/data/options.json")

# The full prompt this add-on used to ship as the `system_prompt` default,
# before the base prompt became code-owned (see app/agent/factory.py
# _BASE_PROMPT). Kept only so `_legacy_keys` can recognise an options.json
# that still carries this exact text and skip folding it into
# extra_instructions (it would otherwise duplicate the code-owned prompt).
_OLD_SHIPPED_PROMPT = (
    "You are an assistant for this Home Assistant instance. "
    "Answer questions about the home using the available tools, and always "
    "look up real data with tools instead of guessing. "
    "Real-world actions (turning on lights, starting the vacuum, etc.) can "
    "only be performed by triggering automations — you cannot control devices "
    "directly. "
    "When the user asks you to perform an action now, first thing: look up the "
    "available actions and trigger the one that matches the request. "
    "If none matches, tell the user the action is not possible yet. Do not "
    "investigate further. "
    "If the user wants something to happen later (when something happens, or at "
    "a time), schedule it instead - never tell them to create an automation. "
    "Once a tool returns an answer, do not overthink it — present the "
    "information as you found it. "
    "Be concise and factual, follow instructions precisely, and do not do "
    "anything extra that wasn't asked."
)


def _map_legacy_keys(data):
    # Keys from pre-0.4.25 options.json files. The Supervisor only keeps
    # them because config.yaml lists them as hidden schema entries, and it
    # always merges in the shipped `allow_actions: false` default — so a
    # legacy key must count even when the new key is present.
    if not isinstance(data, dict):
        return data
    data = dict(data)

    # max_tier (1|2): tier 2 turns actions on, OR-ed with allow_actions.
    if "max_tier" in data:
        legacy_tier = data.pop("max_tier")
        actions_on = bool(data.get("allow_actions"))
        if legacy_tier is not None and int(legacy_tier) >= 2:
            actions_on = True
        data["allow_actions"] = actions_on

    # needle_enabled: OR-ed into fast_path_enabled.
    if "needle_enabled" in data:
        legacy_enabled = data.pop("needle_enabled")
        if legacy_enabled:
            data["fast_path_enabled"] = True

    # needle_remote_url / needle_confidence_threshold: used only when the
    # new key is absent, empty or 0.
    legacy_pairs = [
        ("needle_remote_url", "fast_path_url"),
        ("needle_confidence_threshold", "fast_path_confidence_threshold"),
    ]
    for legacy_key, new_key in legacy_pairs:
        if legacy_key not in data:
            continue
        legacy_value = data.pop(legacy_key)
        if not data.get(new_key) and legacy_value:
            data[new_key] = legacy_value

    # system_prompt: folded into extra_instructions when that is absent or
    # empty, unless it is exactly the old shipped default (which would
    # otherwise duplicate the code-owned base prompt).
    if "system_prompt" in data:
        legacy_prompt = data.pop("system_prompt")
        if legacy_prompt and not data.get("extra_instructions"):
            normalised = " ".join(str(legacy_prompt).split())
            old_normalised = " ".join(_OLD_SHIPPED_PROMPT.split())
            if normalised != old_normalised:
                data["extra_instructions"] = legacy_prompt
                logging.getLogger("config").warning(
                    "system_prompt is deprecated; using it as extra_instructions"
                )
    return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def __init__(self, **values):
        # Map legacy keys on the raw kwargs (options.json) before the init
        # source runs: it collapses fast_path_* and their needle_* aliases
        # into one key, which would hide the legacy value from the validator.
        super().__init__(**_map_legacy_keys(values))

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data):
        # Second pass for values from env / .env (NEEDLE_* via the aliases).
        return _map_legacy_keys(data)

    # Home Assistant
    ha_base_url: str = "http://supervisor/core"
    ha_token: str = Field(
        default="", validation_alias=AliasChoices("SUPERVISOR_TOKEN", "HA_TOKEN")
    )
    ws_connect_timeout: float = 10.0

    # LLM backend
    # "auto" (default) probes llm_url to pick ollama/llamacpp/openai; see
    # app/agent/llm_probe.py. Set explicitly to skip the probe.
    llm_provider: str = "auto"  # "ollama", "llamacpp" (llama-server HTTP API), or any litellm provider
    llm_url: str = Field(
        "http://localhost:11434",
        validation_alias=AliasChoices("LLM_URL"),
    )
    llm_model: str = "hf.co/empero-ai/Qwen3.8-2B-GGUF:Q6_K"
    api_key: str = ""  # cloud provider key, used only by litellm providers

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
    fast_path_enabled: bool = Field(
        False, validation_alias=AliasChoices("fast_path_enabled", "needle_enabled"))
    fast_path_url: str = Field(
        "", validation_alias=AliasChoices("fast_path_url", "needle_remote_url"))
    # Fire only at or above this confidence. Keep 0 unless your model reports a
    # calibrated confidence (e.g. a model fine-tuned on Needle's site).
    fast_path_confidence_threshold: float = Field(
        0.0, validation_alias=AliasChoices("fast_path_confidence_threshold",
                                           "needle_confidence_threshold"))

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
