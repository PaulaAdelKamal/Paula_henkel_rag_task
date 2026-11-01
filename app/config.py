"""Single source of truth for configuration.

Every setting is read from the environment (or a local ``.env`` file in
development). Secrets are typed as ``SecretStr`` so they are masked in reprs,
logs and tracebacks. Everything that is not a secret has a sensible default,
so the grader only has to paste keys into ``.env``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `KEY=` (empty) in .env / --env-file means "use the default", not "empty string".
        env_ignore_empty=True,
    )

    # --- Runtime: required secrets ---
    openrouter_api_key: SecretStr
    qdrant_url: str
    qdrant_api_key: SecretStr

    # --- Runtime: models (provisional defaults until the Step 9/10 benchmark) ---
    chat_model: str = "openai/gpt-4.1-mini"
    embedding_model: str = "qwen/qwen3-embedding-8b"

    # --- Runtime: retrieval ---
    qdrant_collection: str | None = None  # optional override of the derived name
    top_k: int = Field(default=5, ge=1, le=50)
    relevance_threshold: float = Field(default=0.0, ge=-1.0, le=1.0)

    # --- Runtime: observability (optional; the app must work without it) ---
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # --- Runtime: server ---
    app_port: int = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Offline only (ingestion / evaluation), never needed by the grader ---
    vision_model: str | None = None
    judge_model: str | None = None
    eval_gen_model: str | None = None
    cache_dir: str = "data/cache"
    max_spend_usd: float = 5.0

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


class ConfigError(RuntimeError):
    """Human-readable configuration error. Never contains secret values."""


def describe_validation_error(exc: ValidationError) -> str:
    """Name the offending variables without echoing their (possibly secret) values."""
    lines = []
    for err in exc.errors():
        var = str(err["loc"][0]).upper() if err["loc"] else "<unknown>"
        if err["type"] == "missing":
            lines.append(f"- {var} is missing (see .env.example)")
        else:
            lines.append(f"- {var} is invalid: {err['msg']}")
    return "Configuration error:\n" + "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        raise ConfigError(describe_validation_error(exc)) from None
