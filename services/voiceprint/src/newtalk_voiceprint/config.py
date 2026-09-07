import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoicePrintConfig:
    host: str = "0.0.0.0"
    port: int = 8010
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://newtalk:newtalk@127.0.0.1:5432/newtalk"
    api_token: str = "local-voiceprint-token"
    backend: str = "deterministic"
    model_id: str = "iic/speech_campplus_sv_zh-cn_3dspeaker_16k"
    device: str = "cpu"
    similarity_threshold: float = 0.72
    min_sample_seconds: float = 2.0
    max_sample_seconds: float = 15.0
    max_audio_bytes: int = 1_000_000

    @classmethod
    def from_environment(cls) -> "VoicePrintConfig":
        defaults = cls()
        config = cls(
            host=os.getenv("VOICEPRINT_HOST", defaults.host),
            port=int(os.getenv("VOICEPRINT_PORT", str(defaults.port))),
            log_level=os.getenv("VOICEPRINT_LOG_LEVEL", defaults.log_level).upper(),
            database_url=os.getenv("VOICEPRINT_DATABASE_URL", defaults.database_url),
            api_token=os.getenv("VOICEPRINT_API_TOKEN", defaults.api_token),
            backend=os.getenv("VOICEPRINT_BACKEND", defaults.backend).lower(),
            model_id=os.getenv("VOICEPRINT_MODEL_ID", defaults.model_id),
            device=os.getenv("VOICEPRINT_DEVICE", defaults.device).lower(),
            similarity_threshold=float(
                os.getenv(
                    "VOICEPRINT_SIMILARITY_THRESHOLD",
                    str(defaults.similarity_threshold),
                )
            ),
            min_sample_seconds=float(
                os.getenv("VOICEPRINT_MIN_SAMPLE_SECONDS", str(defaults.min_sample_seconds))
            ),
            max_sample_seconds=float(
                os.getenv("VOICEPRINT_MAX_SAMPLE_SECONDS", str(defaults.max_sample_seconds))
            ),
            max_audio_bytes=int(
                os.getenv("VOICEPRINT_MAX_AUDIO_BYTES", str(defaults.max_audio_bytes))
            ),
        )
        if config.backend not in {"campplus", "deterministic"}:
            raise ValueError("VOICEPRINT_BACKEND must be campplus or deterministic")
        if not config.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("VOICEPRINT_DATABASE_URL must use postgresql+asyncpg://")
        if not config.api_token:
            raise ValueError("VOICEPRINT_API_TOKEN must not be empty")
        if not 0 < config.similarity_threshold <= 1:
            raise ValueError("VOICEPRINT_SIMILARITY_THRESHOLD must be in (0, 1]")
        if not 0 < config.min_sample_seconds < config.max_sample_seconds:
            raise ValueError("VoicePrint sample duration bounds are invalid")
        return config
