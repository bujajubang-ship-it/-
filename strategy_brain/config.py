from __future__ import annotations

import os
from dataclasses import dataclass


ALLOWED_PROVIDERS = frozenset({"anthropic", "openai"})
ALLOWED_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BrainSettings:
    """Runtime settings for the shared strategy brain and rollback provider."""

    provider: str = "openai"
    fallback_provider: str = "anthropic"
    openai_model: str = "gpt-5.6-sol"
    reasoning_effort: str = "max"
    store_responses: bool = False
    max_tool_rounds: int = 8

    @classmethod
    def from_env(cls) -> "BrainSettings":
        settings = cls(
            provider=os.getenv("STRATEGY_BRAIN_PROVIDER", "openai").strip().lower(),
            fallback_provider=os.getenv(
                "STRATEGY_BRAIN_FALLBACK_PROVIDER", "anthropic"
            ).strip().lower(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-sol").strip(),
            reasoning_effort=os.getenv(
                "OPENAI_REASONING_EFFORT", "max"
            ).strip().lower(),
            store_responses=_env_bool("OPENAI_STORE_RESPONSES", False),
            max_tool_rounds=int(os.getenv("OPENAI_MAX_TOOL_ROUNDS", "8")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.provider not in ALLOWED_PROVIDERS:
            raise ValueError(
                "STRATEGY_BRAIN_PROVIDER must be 'anthropic' or 'openai'."
            )
        if self.fallback_provider not in ALLOWED_PROVIDERS:
            raise ValueError(
                "STRATEGY_BRAIN_FALLBACK_PROVIDER must be 'anthropic' or 'openai'."
            )
        if not self.openai_model:
            raise ValueError("OPENAI_MODEL must not be empty.")
        if self.reasoning_effort not in ALLOWED_REASONING_EFFORTS:
            allowed = ", ".join(sorted(ALLOWED_REASONING_EFFORTS))
            raise ValueError(f"OPENAI_REASONING_EFFORT must be one of: {allowed}.")
        if not 1 <= self.max_tool_rounds <= 20:
            raise ValueError("OPENAI_MAX_TOOL_ROUNDS must be between 1 and 20.")
