import logging

import pytest

from newtalk.config import AppConfig, ConfigError, DEFAULT_WEB_ROOT, load_config
from newtalk.logging_config import configure_logging


def test_config_uses_p1_defaults() -> None:
    config = load_config({})

    assert config == AppConfig(
        host="127.0.0.1",
        port=8006,
        log_level="INFO",
        web_root=DEFAULT_WEB_ROOT,
    )


def test_config_reads_environment_values(tmp_path) -> None:
    config = load_config(
        {
            "NEWTALK_HOST": "0.0.0.0",
            "NEWTALK_PORT": "9000",
            "NEWTALK_LOG_LEVEL": "debug",
            "NEWTALK_WEB_ROOT": str(tmp_path),
            "NEWTALK_LLM_BACKEND": "openai",
            "NEWTALK_LLM_API_KEY": "test-secret",
            "NEWTALK_LLM_BASE_URL": "https://example.test/v1",
            "NEWTALK_LLM_MODEL": "test-model",
            "NEWTALK_LLM_SYSTEM_PROMPT": "Test system prompt",
            "NEWTALK_LLM_TIMEOUT_SECONDS": "12.5",
        }
    )

    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.log_level == "DEBUG"
    assert config.web_root == tmp_path.resolve()
    assert config.llm_backend == "openai"
    assert config.llm_api_key == "test-secret"
    assert config.llm_base_url == "https://example.test/v1"
    assert config.llm_model == "test-model"
    assert config.llm_system_prompt == "Test system prompt"
    assert config.llm_timeout_seconds == 12.5
    assert "test-secret" not in repr(config)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NEWTALK_HOST", ""),
        ("NEWTALK_PORT", "not-a-port"),
        ("NEWTALK_PORT", "0"),
        ("NEWTALK_LOG_LEVEL", "verbose"),
        ("NEWTALK_LLM_BACKEND", "unknown"),
        ("NEWTALK_LLM_TIMEOUT_SECONDS", "never"),
        ("NEWTALK_LLM_TIMEOUT_SECONDS", "0"),
    ],
)
def test_config_rejects_invalid_values(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_config({name: value})


@pytest.mark.parametrize(
    ("values", "missing_name"),
    [
        (
            {"NEWTALK_LLM_BACKEND": "openai", "NEWTALK_LLM_MODEL": "model"},
            "NEWTALK_LLM_API_KEY",
        ),
        (
            {"NEWTALK_LLM_BACKEND": "openai", "NEWTALK_LLM_API_KEY": "key"},
            "NEWTALK_LLM_MODEL",
        ),
    ],
)
def test_openai_config_requires_secret_and_model(
    values: dict[str, str], missing_name: str
) -> None:
    with pytest.raises(ConfigError, match=missing_name):
        load_config(values)


def test_logging_configures_newtalk_namespace() -> None:
    logger = configure_logging("DEBUG")

    assert logger.name == "newtalk"
    assert logger.level == logging.DEBUG
    assert logger.handlers

    configure_logging("INFO")
