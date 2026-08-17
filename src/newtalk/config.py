import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8006
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LLM_BACKEND = "fake"
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
VALID_LLM_BACKENDS = {"fake", "openai"}


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    web_root: Path = DEFAULT_WEB_ROOT
    llm_backend: str = DEFAULT_LLM_BACKEND
    llm_api_key: str | None = field(default=None, repr=False)
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_system_prompt: str | None = None
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "AppConfig":
        host = values.get("NEWTALK_HOST", DEFAULT_HOST).strip()
        if not host:
            raise ConfigError("NEWTALK_HOST must not be empty")

        raw_port = values.get("NEWTALK_PORT", str(DEFAULT_PORT)).strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigError("NEWTALK_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigError("NEWTALK_PORT must be between 1 and 65535")

        log_level = values.get("NEWTALK_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
        if log_level not in VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ConfigError(f"NEWTALK_LOG_LEVEL must be one of: {allowed}")

        raw_web_root = values.get("NEWTALK_WEB_ROOT")
        web_root = (
            Path(raw_web_root).expanduser().resolve()
            if raw_web_root
            else DEFAULT_WEB_ROOT
        )

        llm_backend = values.get("NEWTALK_LLM_BACKEND", DEFAULT_LLM_BACKEND).strip().lower()
        if llm_backend not in VALID_LLM_BACKENDS:
            allowed = ", ".join(sorted(VALID_LLM_BACKENDS))
            raise ConfigError(f"NEWTALK_LLM_BACKEND must be one of: {allowed}")

        llm_api_key = _optional_value(values.get("NEWTALK_LLM_API_KEY"))
        llm_base_url = _optional_value(values.get("NEWTALK_LLM_BASE_URL"))
        llm_model = _optional_value(values.get("NEWTALK_LLM_MODEL"))
        llm_system_prompt = _optional_value(values.get("NEWTALK_LLM_SYSTEM_PROMPT"))

        raw_timeout = values.get(
            "NEWTALK_LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS)
        ).strip()
        try:
            llm_timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise ConfigError("NEWTALK_LLM_TIMEOUT_SECONDS must be a number") from exc
        if llm_timeout_seconds <= 0:
            raise ConfigError("NEWTALK_LLM_TIMEOUT_SECONDS must be greater than zero")

        if llm_backend == "openai":
            if not llm_api_key:
                raise ConfigError(
                    "NEWTALK_LLM_API_KEY is required when NEWTALK_LLM_BACKEND=openai"
                )
            if not llm_model:
                raise ConfigError(
                    "NEWTALK_LLM_MODEL is required when NEWTALK_LLM_BACKEND=openai"
                )

        return cls(
            host=host,
            port=port,
            log_level=log_level,
            web_root=web_root,
            llm_backend=llm_backend,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_system_prompt=llm_system_prompt,
            llm_timeout_seconds=llm_timeout_seconds,
        )


def load_config(values: Mapping[str, str] | None = None) -> AppConfig:
    if values is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        values = os.environ
    return AppConfig.from_mapping(values)


def _optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
