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
        }
    )

    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.log_level == "DEBUG"
    assert config.web_root == tmp_path.resolve()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NEWTALK_HOST", ""),
        ("NEWTALK_PORT", "not-a-port"),
        ("NEWTALK_PORT", "0"),
        ("NEWTALK_LOG_LEVEL", "verbose"),
    ],
)
def test_config_rejects_invalid_values(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_config({name: value})


def test_logging_configures_newtalk_namespace() -> None:
    logger = configure_logging("DEBUG")

    assert logger.name == "newtalk"
    assert logger.level == logging.DEBUG
    assert logger.handlers

    configure_logging("INFO")
