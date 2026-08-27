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
            "NEWTALK_TTS_BACKEND": "doubao",
            "NEWTALK_TTS_APP_ID": "tts-app",
            "NEWTALK_TTS_ACCESS_TOKEN": "tts-secret",
            "NEWTALK_TTS_RESOURCE_ID": "seed-tts-2.0",
            "NEWTALK_TTS_VOICE_TYPE": "test-voice",
            "NEWTALK_TTS_AUDIO_FORMAT": "pcm",
            "NEWTALK_TTS_SAMPLE_RATE": "16000",
            "NEWTALK_TTS_TIMEOUT_SECONDS": "9.5",
            "NEWTALK_TTS_USE_SYSTEM_PROXY": "true",
            "NEWTALK_VAD_MODEL_PATH": str(tmp_path / "vad.onnx"),
            "NEWTALK_VAD_THRESHOLD": "0.6",
            "NEWTALK_VAD_THRESHOLD_LOW": "0.2",
            "NEWTALK_VAD_MIN_SILENCE_MS": "450",
            "NEWTALK_VAD_PRE_ROLL_MS": "240",
            "NEWTALK_ASR_BACKEND": "doubao",
            "NEWTALK_ASR_FAKE_TEXT": "fake speech",
            "NEWTALK_ASR_API_KEY": "asr-secret",
            "NEWTALK_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration",
            "NEWTALK_ASR_WS_URL": "wss://example.test/asr",
            "NEWTALK_ASR_PACKET_DURATION_MS": "120",
            "NEWTALK_ASR_TIMEOUT_SECONDS": "8.5",
            "NEWTALK_ASR_USE_SYSTEM_PROXY": "true",
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
    assert config.tts_backend == "doubao"
    assert config.tts_app_id == "tts-app"
    assert config.tts_access_token == "tts-secret"
    assert config.tts_resource_id == "seed-tts-2.0"
    assert config.tts_voice_type == "test-voice"
    assert config.tts_sample_rate == 16000
    assert config.tts_timeout_seconds == 9.5
    assert config.tts_use_system_proxy is True
    assert config.vad_model_path == (tmp_path / "vad.onnx").resolve()
    assert config.vad_threshold == 0.6
    assert config.vad_threshold_low == 0.2
    assert config.vad_min_silence_ms == 450
    assert config.vad_pre_roll_ms == 240
    assert config.asr_fake_text == "fake speech"
    assert config.asr_backend == "doubao"
    assert config.asr_api_key == "asr-secret"
    assert config.asr_resource_id == "volc.seedasr.sauc.duration"
    assert config.asr_ws_url == "wss://example.test/asr"
    assert config.asr_packet_duration_ms == 120
    assert config.asr_timeout_seconds == 8.5
    assert config.asr_use_system_proxy is True
    assert "test-secret" not in repr(config)
    assert "tts-secret" not in repr(config)
    assert "asr-secret" not in repr(config)


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
        ("NEWTALK_TTS_BACKEND", "unknown"),
        ("NEWTALK_TTS_WS_URL", "https://not-websocket.test"),
        ("NEWTALK_TTS_AUDIO_FORMAT", "mp3"),
        ("NEWTALK_TTS_SAMPLE_RATE", "44100"),
        ("NEWTALK_TTS_TIMEOUT_SECONDS", "0"),
        ("NEWTALK_TTS_USE_SYSTEM_PROXY", "sometimes"),
        ("NEWTALK_VAD_THRESHOLD", "loud"),
        ("NEWTALK_VAD_THRESHOLD", "1.1"),
        ("NEWTALK_VAD_THRESHOLD_LOW", "0.5"),
        ("NEWTALK_VAD_MIN_SILENCE_MS", "0"),
        ("NEWTALK_VAD_PRE_ROLL_MS", "none"),
        ("NEWTALK_ASR_BACKEND", "unknown"),
        ("NEWTALK_ASR_FAKE_TEXT", ""),
        ("NEWTALK_ASR_WS_URL", "https://not-websocket.test"),
        ("NEWTALK_ASR_PACKET_DURATION_MS", "0"),
        ("NEWTALK_ASR_PACKET_DURATION_MS", "1001"),
        ("NEWTALK_ASR_TIMEOUT_SECONDS", "0"),
        ("NEWTALK_ASR_USE_SYSTEM_PROXY", "sometimes"),
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


@pytest.mark.parametrize(
    "missing_name",
    [
        "NEWTALK_TTS_APP_ID",
        "NEWTALK_TTS_ACCESS_TOKEN",
        "NEWTALK_TTS_RESOURCE_ID",
        "NEWTALK_TTS_VOICE_TYPE",
    ],
)
def test_doubao_config_requires_all_provider_values(missing_name: str) -> None:
    values = {
        "NEWTALK_TTS_BACKEND": "doubao",
        "NEWTALK_TTS_APP_ID": "app",
        "NEWTALK_TTS_ACCESS_TOKEN": "token",
        "NEWTALK_TTS_RESOURCE_ID": "resource",
        "NEWTALK_TTS_VOICE_TYPE": "voice",
    }
    del values[missing_name]

    with pytest.raises(ConfigError, match=missing_name):
        load_config(values)


@pytest.mark.parametrize(
    "missing_name",
    ["NEWTALK_ASR_API_KEY", "NEWTALK_ASR_RESOURCE_ID"],
)
def test_doubao_asr_config_requires_provider_values(missing_name: str) -> None:
    values = {
        "NEWTALK_ASR_BACKEND": "doubao",
        "NEWTALK_ASR_API_KEY": "key",
        "NEWTALK_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration",
    }
    del values[missing_name]

    with pytest.raises(ConfigError, match=missing_name):
        load_config(values)


def test_logging_configures_newtalk_namespace() -> None:
    logger = configure_logging("DEBUG")

    assert logger.name == "newtalk"
    assert logger.level == logging.DEBUG
    assert logger.handlers

    configure_logging("INFO")
