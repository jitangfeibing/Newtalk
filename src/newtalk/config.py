import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8006
DEFAULT_LOG_LEVEL = "INFO"
VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    web_root: Path = DEFAULT_WEB_ROOT

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
        return cls(host=host, port=port, log_level=log_level, web_root=web_root)


def load_config(values: Mapping[str, str] | None = None) -> AppConfig:
    if values is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        values = os.environ
    return AppConfig.from_mapping(values)
