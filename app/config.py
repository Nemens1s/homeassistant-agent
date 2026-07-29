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
    "Be concise and factual."
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
    llm_provider: str = "ollama"  # "ollama" or a litellm provider, e.g. "anthropic"
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
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
    allowed_domains: list[str] = ["light", "switch", "automation"]
    recursion_limit: int = 15
    max_rows: int = 50

    @property
    def ws_url(self) -> str:
        base = self.ha_base_url.replace("http://", "ws://").replace("https://", "wss://")
        if base.endswith("/core"):
            return f"{base}/websocket"  # supervisor proxy path
        return f"{base}/api/websocket"  # direct HA instance


def load_settings() -> Settings:
    if OPTIONS_FILE.exists():
        data = json.loads(OPTIONS_FILE.read_text())
        return Settings(**{k: v for k, v in data.items() if v is not None})
    return Settings()
