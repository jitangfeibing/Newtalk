from contextlib import asynccontextmanager
from dataclasses import replace
from functools import lru_cache
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from newtalk import __version__
from newtalk.asr import DoubaoStreamingASR, FakeASR, SpeechRecognizer
from newtalk.audio import SileroVad, VoiceActivityDetector
from newtalk.chat import ChatService, FakeLLM, OpenAICompatibleChatModel
from newtalk.config import AppConfig, load_config
from newtalk.logging_config import configure_logging
from newtalk.identity import IdentityService, SqlAlchemyIdentityStore
from newtalk.identity.api import RecoveryRateLimiter, router as identity_router
from newtalk.transport import websocket_router
from newtalk.tts import DoubaoTTS, FakeTTS, TextToSpeech


logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _create_silero_vad(
    model_path: Path,
    threshold: float,
    threshold_low: float,
    min_silence_ms: int,
) -> SileroVad:
    return SileroVad(
        model_path,
        threshold=threshold,
        threshold_low=threshold_low,
        min_silence_duration_ms=min_silence_ms,
    )


def create_vad(config: AppConfig) -> VoiceActivityDetector:
    return _create_silero_vad(
        config.vad_model_path,
        config.vad_threshold,
        config.vad_threshold_low,
        config.vad_min_silence_ms,
    )


def create_recognizer(config: AppConfig) -> SpeechRecognizer:
    if config.asr_backend == "fake":
        return FakeASR(config.asr_fake_text)
    if not config.asr_api_key or not config.asr_resource_id:
        raise RuntimeError("Doubao ASR configuration is incomplete")
    return DoubaoStreamingASR(
        api_key=config.asr_api_key,
        resource_id=config.asr_resource_id,
        ws_url=config.asr_ws_url,
        packet_duration_ms=config.asr_packet_duration_ms,
        timeout_seconds=config.asr_timeout_seconds,
        use_system_proxy=config.asr_use_system_proxy,
    )


def create_synthesizer(config: AppConfig) -> TextToSpeech:
    if config.tts_backend == "fake":
        return FakeTTS(sample_rate=config.tts_sample_rate)

    if not all(
        (
            config.tts_app_id,
            config.tts_access_token,
            config.tts_resource_id,
            config.tts_voice_type,
        )
    ):
        raise RuntimeError("Doubao TTS configuration is incomplete")
    return DoubaoTTS(
        app_id=config.tts_app_id,
        access_token=config.tts_access_token,
        resource_id=config.tts_resource_id,
        voice_type=config.tts_voice_type,
        ws_url=config.tts_ws_url,
        sample_rate=config.tts_sample_rate,
        timeout_seconds=config.tts_timeout_seconds,
        use_system_proxy=config.tts_use_system_proxy,
    )


def create_chat_service(config: AppConfig) -> ChatService:
    synthesizer = create_synthesizer(config)
    if config.llm_backend == "fake":
        return ChatService(FakeLLM(), synthesizer)

    if not config.llm_api_key or not config.llm_model:
        raise RuntimeError("OpenAI-compatible LLM configuration is incomplete")
    return ChatService(
        OpenAICompatibleChatModel(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            model=config.llm_model,
            system_prompt=config.llm_system_prompt,
            timeout_seconds=config.llm_timeout_seconds,
        ),
        synthesizer,
    )


def create_identity_service(config: AppConfig) -> IdentityService:
    return IdentityService(SqlAlchemyIdentityStore(config.database_url))


def create_app(
    config: AppConfig | None = None,
    *,
    web_root: Path | None = None,
    chat_service: ChatService | None = None,
    vad: VoiceActivityDetector | None = None,
    recognizer: SpeechRecognizer | None = None,
    identity_service: IdentityService | None = None,
) -> FastAPI:
    config = config or load_config()
    if web_root is not None:
        config = replace(config, web_root=web_root)
    if not config.web_root.is_dir():
        raise RuntimeError(f"Web root does not exist: {config.web_root}")
    resolved_chat_service = chat_service or create_chat_service(config)
    resolved_vad = vad or create_vad(config)
    resolved_recognizer = recognizer or create_recognizer(config)
    resolved_identity_service = identity_service or create_identity_service(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await resolved_identity_service.start()
            logger.info(
                "service_started host=%s port=%s web_root=%s llm_backend=%s llm_model=%s tts_backend=%s asr_backend=%s identity_service=%s",
                config.host,
                config.port,
                config.web_root,
                config.llm_backend,
                config.llm_model or "fake",
                config.tts_backend,
                config.asr_backend,
                type(resolved_identity_service).__name__,
            )
            yield
        finally:
            await resolved_chat_service.aclose()
            await resolved_recognizer.aclose()
            await resolved_identity_service.close()
            logger.info("service_stopped")

    app = FastAPI(title="Newtalk", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.chat_service = resolved_chat_service
    app.state.vad = resolved_vad
    app.state.recognizer = resolved_recognizer
    app.state.identity_service = resolved_identity_service
    app.state.recovery_rate_limiter = RecoveryRateLimiter(
        max_attempts=config.recovery_max_attempts,
        window_seconds=config.recovery_window_seconds,
    )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "newtalk",
            "version": __version__,
        }

    @app.get("/ready", tags=["system"])
    async def readiness() -> dict[str, str]:
        try:
            await resolved_identity_service.ping()
        except Exception as exc:
            logger.warning("readiness_failed dependency=database error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ready", "database": "ok"}

    app.include_router(identity_router)
    app.include_router(websocket_router)

    app.mount(
        "/",
        StaticFiles(directory=config.web_root, html=True),
        name="web",
    )

    return app


runtime_config = load_config()
configure_logging(runtime_config.log_level)
app = create_app(
    runtime_config,
    identity_service=create_identity_service(runtime_config),
)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level.lower(),
    )
