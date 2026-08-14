"""Settings. Every deployment knob comes from the environment / `.env`.

Adapted from `draftsmith/server/draftsmith/config.py` — the stage-routing and
provider-fallback logic is proven, so it is reused rather than reinvented. The
stage names are CADenza's four agent personas (blueprint §3) instead of
Draftsmith's pipeline stages.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parents[1]      # cadenza/backend
_CADENZA_DIR = _BACKEND_DIR.parent                       # cadenza

# Later entries win in pydantic-settings, so a backend-local .env can override
# the shared one during local experiments without editing the shared file.
ENV_FILES = (_CADENZA_DIR / ".env", _BACKEND_DIR / ".env")

PROVIDERS = ("anthropic", "deepseek", "openrouter")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[str(p) for p in ENV_FILES],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── AI providers ────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    # ── Agent routing: "provider:model" ─────────────────────────────────────
    # Spend where a wrong answer is expensive. The Machinist writes patches that
    # mutate the single source of truth and the Engineer invents the numbers the
    # part is actually cut to, so both get the strongest model. Routing a payload
    # to one of four names is mechanical, so the Coordinator gets the cheap one.
    model_coordinator: str = "deepseek:deepseek-chat"
    model_draftsman: str = "anthropic:claude-sonnet-5"   # vision: reads sketches
    model_engineer: str = "anthropic:claude-opus-5"
    model_machinist: str = "anthropic:claude-opus-5"

    # Effort is per-request on Anthropic models; "high" is the API default and
    # the setting these calls run at. `xhigh` is available and is the right dial
    # if patch quality ever proves marginal — but it wants max_tokens >= 64000
    # and therefore streaming, which this non-streaming call surface does not do.
    #
    # max_tokens caps thinking AND response text together, and thinking is on by
    # default on Opus 5 — so this is sized well above what a patch payload needs.
    machinist_effort: str = "high"
    agent_max_tokens: int = 16000

    # ── API ─────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # ── Sessions ────────────────────────────────────────────────────────────
    session_ttl_seconds: int = 1800
    max_sessions: int = 200

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ── provider plumbing ───────────────────────────────────────────────────

    def stage_model(self, stage: str) -> tuple[str, str]:
        """(provider, model) for an agent, falling back when the key is missing."""
        raw = {
            "coordinator": self.model_coordinator,
            "draftsman": self.model_draftsman,
            "engineer": self.model_engineer,
            "machinist": self.model_machinist,
        }.get(stage, self.model_machinist)
        provider, _, model = raw.partition(":")
        if self.key_for(provider):
            return (provider, model)

        # Configured provider has no key — fall back to one that does rather
        # than failing a request the user could have had answered.
        if self.anthropic_api_key:
            return ("anthropic", "claude-sonnet-5" if stage == "coordinator" else "claude-opus-5")
        if self.deepseek_api_key:
            return ("deepseek", "deepseek-chat")
        if self.openrouter_api_key:
            return (
                "openrouter",
                "deepseek/deepseek-chat" if stage == "coordinator" else "anthropic/claude-opus-4.1",
            )
        return (provider, model)

    def key_for(self, provider: str) -> str:
        return {
            "anthropic": self.anthropic_api_key,
            "deepseek": self.deepseek_api_key,
            "openrouter": self.openrouter_api_key,
        }.get(provider, "")

    def base_url_for(self, provider: str) -> str:
        return {
            "deepseek": self.deepseek_base_url,
            "openrouter": self.openrouter_base_url,
        }.get(provider, self.openrouter_base_url)

    @property
    def ai_available(self) -> bool:
        return bool(self.anthropic_api_key or self.deepseek_api_key or self.openrouter_api_key)

    @property
    def vision_available(self) -> bool:
        """Image input needs a provider we know accepts it. DeepSeek's chat endpoint is text-only."""
        return bool(self.anthropic_api_key or self.openrouter_api_key)

    def configured_providers(self) -> list[str]:
        return [p for p in PROVIDERS if self.key_for(p)]


@lru_cache
def get_settings() -> Settings:
    return Settings()
