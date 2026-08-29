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
DEFAULT_DIALOGUE_MAX_TURNS = 8
DEFAULT_DIALOGUE_MAX_CHARS = 12000
DEFAULT_LLM_BACKEND = "fake"
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
DEFAULT_TTS_BACKEND = "fake"
DEFAULT_TTS_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DEFAULT_TTS_AUDIO_FORMAT = "pcm"
DEFAULT_TTS_SAMPLE_RATE = 24000
DEFAULT_TTS_TIMEOUT_SECONDS = 30.0
DEFAULT_TTS_USE_SYSTEM_PROXY = False
DEFAULT_VAD_MODEL_PATH = PROJECT_ROOT / "models" / "silero_vad.onnx"
DEFAULT_VAD_THRESHOLD = 0.5
DEFAULT_VAD_THRESHOLD_LOW = 0.3
DEFAULT_VAD_MIN_SILENCE_MS = 600
DEFAULT_VAD_PRE_ROLL_MS = 300
DEFAULT_ASR_BACKEND = "fake"
DEFAULT_ASR_FAKE_TEXT = "这是一次语音输入测试"
DEFAULT_ASR_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_ASR_PACKET_DURATION_MS = 100
DEFAULT_ASR_TIMEOUT_SECONDS = 30.0
DEFAULT_ASR_USE_SYSTEM_PROXY = False
DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://newtalk:newtalk@127.0.0.1:5432/newtalk"
)
DEFAULT_DEVICE_COOKIE_NAME = "newtalk_device"
DEFAULT_DEVICE_COOKIE_SECURE = False
DEFAULT_DEVICE_COOKIE_MAX_AGE_DAYS = 365
DEFAULT_RECOVERY_MAX_ATTEMPTS = 5
DEFAULT_RECOVERY_WINDOW_SECONDS = 900
VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
VALID_LLM_BACKENDS = {"fake", "openai"}
VALID_TTS_BACKENDS = {"fake", "doubao"}
VALID_TTS_SAMPLE_RATES = {8000, 16000, 24000, 48000}
VALID_ASR_BACKENDS = {"doubao", "fake"}


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    web_root: Path = DEFAULT_WEB_ROOT
    dialogue_max_turns: int = DEFAULT_DIALOGUE_MAX_TURNS
    dialogue_max_chars: int = DEFAULT_DIALOGUE_MAX_CHARS
    llm_backend: str = DEFAULT_LLM_BACKEND
    llm_api_key: str | None = field(default=None, repr=False)
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_system_prompt: str | None = None
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    tts_backend: str = DEFAULT_TTS_BACKEND
    tts_app_id: str | None = field(default=None, repr=False)
    tts_access_token: str | None = field(default=None, repr=False)
    tts_resource_id: str | None = None
    tts_voice_type: str | None = None
    tts_ws_url: str = DEFAULT_TTS_WS_URL
    tts_audio_format: str = DEFAULT_TTS_AUDIO_FORMAT
    tts_sample_rate: int = DEFAULT_TTS_SAMPLE_RATE
    tts_timeout_seconds: float = DEFAULT_TTS_TIMEOUT_SECONDS
    tts_use_system_proxy: bool = DEFAULT_TTS_USE_SYSTEM_PROXY
    vad_model_path: Path = DEFAULT_VAD_MODEL_PATH
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    vad_threshold_low: float = DEFAULT_VAD_THRESHOLD_LOW
    vad_min_silence_ms: int = DEFAULT_VAD_MIN_SILENCE_MS
    vad_pre_roll_ms: int = DEFAULT_VAD_PRE_ROLL_MS
    asr_backend: str = DEFAULT_ASR_BACKEND
    asr_fake_text: str = DEFAULT_ASR_FAKE_TEXT
    asr_api_key: str | None = field(default=None, repr=False)
    asr_resource_id: str | None = None
    asr_ws_url: str = DEFAULT_ASR_WS_URL
    asr_packet_duration_ms: int = DEFAULT_ASR_PACKET_DURATION_MS
    asr_timeout_seconds: float = DEFAULT_ASR_TIMEOUT_SECONDS
    asr_use_system_proxy: bool = DEFAULT_ASR_USE_SYSTEM_PROXY
    database_url: str = field(default=DEFAULT_DATABASE_URL, repr=False)
    device_cookie_name: str = DEFAULT_DEVICE_COOKIE_NAME
    device_cookie_secure: bool = DEFAULT_DEVICE_COOKIE_SECURE
    device_cookie_max_age_days: int = DEFAULT_DEVICE_COOKIE_MAX_AGE_DAYS
    recovery_max_attempts: int = DEFAULT_RECOVERY_MAX_ATTEMPTS
    recovery_window_seconds: int = DEFAULT_RECOVERY_WINDOW_SECONDS

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

        dialogue_max_turns = _positive_int_value(
            values,
            "NEWTALK_DIALOGUE_MAX_TURNS",
            DEFAULT_DIALOGUE_MAX_TURNS,
        )
        if dialogue_max_turns > 50:
            raise ConfigError("NEWTALK_DIALOGUE_MAX_TURNS must not exceed 50")
        dialogue_max_chars = _positive_int_value(
            values,
            "NEWTALK_DIALOGUE_MAX_CHARS",
            DEFAULT_DIALOGUE_MAX_CHARS,
        )
        if dialogue_max_chars > 100000:
            raise ConfigError("NEWTALK_DIALOGUE_MAX_CHARS must not exceed 100000")

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

        tts_backend = values.get("NEWTALK_TTS_BACKEND", DEFAULT_TTS_BACKEND).strip().lower()
        if tts_backend not in VALID_TTS_BACKENDS:
            allowed = ", ".join(sorted(VALID_TTS_BACKENDS))
            raise ConfigError(f"NEWTALK_TTS_BACKEND must be one of: {allowed}")

        tts_app_id = _optional_value(values.get("NEWTALK_TTS_APP_ID"))
        tts_access_token = _optional_value(values.get("NEWTALK_TTS_ACCESS_TOKEN"))
        tts_resource_id = _optional_value(values.get("NEWTALK_TTS_RESOURCE_ID"))
        tts_voice_type = _optional_value(values.get("NEWTALK_TTS_VOICE_TYPE"))
        tts_ws_url = values.get("NEWTALK_TTS_WS_URL", DEFAULT_TTS_WS_URL).strip()
        if not tts_ws_url.startswith(("ws://", "wss://")):
            raise ConfigError("NEWTALK_TTS_WS_URL must be a WebSocket URL")

        tts_audio_format = values.get(
            "NEWTALK_TTS_AUDIO_FORMAT", DEFAULT_TTS_AUDIO_FORMAT
        ).strip().lower()
        if tts_audio_format != "pcm":
            raise ConfigError("P4 only supports NEWTALK_TTS_AUDIO_FORMAT=pcm")

        raw_sample_rate = values.get(
            "NEWTALK_TTS_SAMPLE_RATE", str(DEFAULT_TTS_SAMPLE_RATE)
        ).strip()
        try:
            tts_sample_rate = int(raw_sample_rate)
        except ValueError as exc:
            raise ConfigError("NEWTALK_TTS_SAMPLE_RATE must be an integer") from exc
        if tts_sample_rate not in VALID_TTS_SAMPLE_RATES:
            allowed = ", ".join(str(value) for value in sorted(VALID_TTS_SAMPLE_RATES))
            raise ConfigError(f"NEWTALK_TTS_SAMPLE_RATE must be one of: {allowed}")

        raw_tts_timeout = values.get(
            "NEWTALK_TTS_TIMEOUT_SECONDS", str(DEFAULT_TTS_TIMEOUT_SECONDS)
        ).strip()
        try:
            tts_timeout_seconds = float(raw_tts_timeout)
        except ValueError as exc:
            raise ConfigError("NEWTALK_TTS_TIMEOUT_SECONDS must be a number") from exc
        if tts_timeout_seconds <= 0:
            raise ConfigError("NEWTALK_TTS_TIMEOUT_SECONDS must be greater than zero")

        tts_use_system_proxy = _boolean_value(
            values.get("NEWTALK_TTS_USE_SYSTEM_PROXY"),
            default=DEFAULT_TTS_USE_SYSTEM_PROXY,
            name="NEWTALK_TTS_USE_SYSTEM_PROXY",
        )

        if tts_backend == "doubao":
            required_tts_values = {
                "NEWTALK_TTS_APP_ID": tts_app_id,
                "NEWTALK_TTS_ACCESS_TOKEN": tts_access_token,
                "NEWTALK_TTS_RESOURCE_ID": tts_resource_id,
                "NEWTALK_TTS_VOICE_TYPE": tts_voice_type,
            }
            for name, value in required_tts_values.items():
                if not value:
                    raise ConfigError(
                        f"{name} is required when NEWTALK_TTS_BACKEND=doubao"
                    )

        raw_vad_model_path = values.get("NEWTALK_VAD_MODEL_PATH")
        vad_model_path = (
            Path(raw_vad_model_path).expanduser().resolve()
            if raw_vad_model_path
            else DEFAULT_VAD_MODEL_PATH
        )
        vad_threshold = _float_value(
            values,
            "NEWTALK_VAD_THRESHOLD",
            DEFAULT_VAD_THRESHOLD,
        )
        vad_threshold_low = _float_value(
            values,
            "NEWTALK_VAD_THRESHOLD_LOW",
            DEFAULT_VAD_THRESHOLD_LOW,
        )
        if not 0 <= vad_threshold_low < vad_threshold <= 1:
            raise ConfigError(
                "VAD thresholds must satisfy 0 <= low < high <= 1"
            )
        vad_min_silence_ms = _positive_int_value(
            values,
            "NEWTALK_VAD_MIN_SILENCE_MS",
            DEFAULT_VAD_MIN_SILENCE_MS,
        )
        vad_pre_roll_ms = _positive_int_value(
            values,
            "NEWTALK_VAD_PRE_ROLL_MS",
            DEFAULT_VAD_PRE_ROLL_MS,
        )

        asr_backend = values.get(
            "NEWTALK_ASR_BACKEND", DEFAULT_ASR_BACKEND
        ).strip().lower()
        if asr_backend not in VALID_ASR_BACKENDS:
            allowed = ", ".join(sorted(VALID_ASR_BACKENDS))
            raise ConfigError(f"NEWTALK_ASR_BACKEND must be one of: {allowed}")
        asr_fake_text = values.get(
            "NEWTALK_ASR_FAKE_TEXT", DEFAULT_ASR_FAKE_TEXT
        ).strip()
        if not asr_fake_text:
            raise ConfigError("NEWTALK_ASR_FAKE_TEXT must not be empty")
        asr_api_key = _optional_value(values.get("NEWTALK_ASR_API_KEY"))
        asr_resource_id = _optional_value(values.get("NEWTALK_ASR_RESOURCE_ID"))
        asr_ws_url = values.get("NEWTALK_ASR_WS_URL", DEFAULT_ASR_WS_URL).strip()
        if not asr_ws_url.startswith(("ws://", "wss://")):
            raise ConfigError("NEWTALK_ASR_WS_URL must be a WebSocket URL")
        asr_packet_duration_ms = _positive_int_value(
            values,
            "NEWTALK_ASR_PACKET_DURATION_MS",
            DEFAULT_ASR_PACKET_DURATION_MS,
        )
        if asr_packet_duration_ms > 1000:
            raise ConfigError("NEWTALK_ASR_PACKET_DURATION_MS must not exceed 1000")
        asr_timeout_seconds = _positive_float_value(
            values,
            "NEWTALK_ASR_TIMEOUT_SECONDS",
            DEFAULT_ASR_TIMEOUT_SECONDS,
        )
        asr_use_system_proxy = _boolean_value(
            values.get("NEWTALK_ASR_USE_SYSTEM_PROXY"),
            default=DEFAULT_ASR_USE_SYSTEM_PROXY,
            name="NEWTALK_ASR_USE_SYSTEM_PROXY",
        )
        if asr_backend == "doubao":
            required_asr_values = {
                "NEWTALK_ASR_API_KEY": asr_api_key,
                "NEWTALK_ASR_RESOURCE_ID": asr_resource_id,
            }
            for name, value in required_asr_values.items():
                if not value:
                    raise ConfigError(
                        f"{name} is required when NEWTALK_ASR_BACKEND=doubao"
                    )

        database_url = values.get(
            "NEWTALK_DATABASE_URL", DEFAULT_DATABASE_URL
        ).strip()
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ConfigError(
                "NEWTALK_DATABASE_URL must use postgresql+asyncpg://"
            )
        device_cookie_name = values.get(
            "NEWTALK_DEVICE_COOKIE_NAME", DEFAULT_DEVICE_COOKIE_NAME
        ).strip()
        if not device_cookie_name or any(
            character.isspace() for character in device_cookie_name
        ):
            raise ConfigError(
                "NEWTALK_DEVICE_COOKIE_NAME must be a non-empty cookie name"
            )
        device_cookie_secure = _boolean_value(
            values.get("NEWTALK_DEVICE_COOKIE_SECURE"),
            default=DEFAULT_DEVICE_COOKIE_SECURE,
            name="NEWTALK_DEVICE_COOKIE_SECURE",
        )
        device_cookie_max_age_days = _positive_int_value(
            values,
            "NEWTALK_DEVICE_COOKIE_MAX_AGE_DAYS",
            DEFAULT_DEVICE_COOKIE_MAX_AGE_DAYS,
        )
        recovery_max_attempts = _positive_int_value(
            values,
            "NEWTALK_RECOVERY_MAX_ATTEMPTS",
            DEFAULT_RECOVERY_MAX_ATTEMPTS,
        )
        recovery_window_seconds = _positive_int_value(
            values,
            "NEWTALK_RECOVERY_WINDOW_SECONDS",
            DEFAULT_RECOVERY_WINDOW_SECONDS,
        )

        return cls(
            host=host,
            port=port,
            log_level=log_level,
            web_root=web_root,
            dialogue_max_turns=dialogue_max_turns,
            dialogue_max_chars=dialogue_max_chars,
            llm_backend=llm_backend,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_system_prompt=llm_system_prompt,
            llm_timeout_seconds=llm_timeout_seconds,
            tts_backend=tts_backend,
            tts_app_id=tts_app_id,
            tts_access_token=tts_access_token,
            tts_resource_id=tts_resource_id,
            tts_voice_type=tts_voice_type,
            tts_ws_url=tts_ws_url,
            tts_audio_format=tts_audio_format,
            tts_sample_rate=tts_sample_rate,
            tts_timeout_seconds=tts_timeout_seconds,
            tts_use_system_proxy=tts_use_system_proxy,
            vad_model_path=vad_model_path,
            vad_threshold=vad_threshold,
            vad_threshold_low=vad_threshold_low,
            vad_min_silence_ms=vad_min_silence_ms,
            vad_pre_roll_ms=vad_pre_roll_ms,
            asr_backend=asr_backend,
            asr_fake_text=asr_fake_text,
            asr_api_key=asr_api_key,
            asr_resource_id=asr_resource_id,
            asr_ws_url=asr_ws_url,
            asr_packet_duration_ms=asr_packet_duration_ms,
            asr_timeout_seconds=asr_timeout_seconds,
            asr_use_system_proxy=asr_use_system_proxy,
            database_url=database_url,
            device_cookie_name=device_cookie_name,
            device_cookie_secure=device_cookie_secure,
            device_cookie_max_age_days=device_cookie_max_age_days,
            recovery_max_attempts=recovery_max_attempts,
            recovery_window_seconds=recovery_window_seconds,
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


def _boolean_value(value: str | None, *, default: bool, name: str) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _float_value(
    values: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw_value = values.get(name, str(default)).strip()
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _positive_int_value(
    values: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _positive_float_value(
    values: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    value = _float_value(values, name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value
