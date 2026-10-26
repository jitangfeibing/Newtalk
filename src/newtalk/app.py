from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import lru_cache
import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from newtalk import __version__
from newtalk.asr import DoubaoStreamingASR, FakeASR, SpeechRecognizer
from newtalk.audio import SileroVad, VoiceActivityDetector
from newtalk.chat import (
    ChatService,
    DialogueCacheCoordinator,
    DialogueStore,
    FakeLLM,
    InMemoryDialogueStore,
    OpenAICompatibleChatModel,
    SqlAlchemyDialogueStore,
)
from newtalk.config import AppConfig, load_config
from newtalk.context_debug import ContextDebugger
from newtalk.context_debug_api import router as context_debug_router
from newtalk.chat.prompts import conversation_system_prompt
from newtalk.logging_config import configure_logging
from newtalk.identity import IdentityService, SqlAlchemyIdentityStore
from newtalk.identity.deletion import (
    IdentityDeletionScheduler,
    IdentityDeletionService,
    InMemoryIdentityDeletionStore,
    SqlAlchemyIdentityDeletionStore,
)
from newtalk.identity.api import router as identity_router
from newtalk.identity.recovery_limit import (
    InMemoryRecoveryRateLimiter,
    RecoveryRateLimiter,
    SqlAlchemyRecoveryRateLimiter,
)
from newtalk.memory import (
    DisabledMemoryProvider,
    DisabledMemoryWriteService,
    InMemoryMemoryJobStore,
    MemoryProvider,
    MemoryWriteService,
    MemoryWriter,
    SqlAlchemyMemoryJobStore,
)
from newtalk.memory.api import router as memory_router
from newtalk.profile import (
    MemosMemoryProvider,
    ProfileCacheCoordinator,
)
from newtalk.transport import websocket_router
from newtalk.tts import DoubaoTTS, FakeTTS, TextToSpeech
from newtalk.voiceprint import DisabledVoicePrintClient, HttpVoicePrintClient, VoicePrintClient
from newtalk.voiceprint.api import router as voiceprint_router
from newtalk.reminders.api import router as reminder_router
from newtalk.reminders.audio import ReminderAudio
from newtalk.reminders.service import ReminderService
from newtalk.reminders.store import InMemoryReminderStore, SqlAlchemyReminderStore
from newtalk.tools.reminder import ReminderTool
from newtalk.tools.music import MusicTool
from newtalk.music.api import router as music_router
from newtalk.music.provider import YaohuMusicProvider
from newtalk.music.service import MusicService
from newtalk.podcasts.api import router as podcast_router
from newtalk.podcasts.provider import YaohuPodcastProvider
from newtalk.podcasts.service import PodcastService
from newtalk.tools.podcasts import PodcastControlTool, PodcastSearchTool


logger = logging.getLogger(__name__)


async def _close_lifecycle_components(
    *components: tuple[str, Callable[[], Awaitable[None]]],
) -> None:
    """Close every component even when an earlier cleanup operation fails."""
    first_error: Exception | None = None
    for name, close in components:
        try:
            await close()
        except Exception as exc:
            # 关闭链不能在第一个异常处中断，否则后续数据库连接和 HTTP Client
            # 会泄漏；全部尝试完成后仍抛出首个异常，让部署平台感知退出失败。
            logger.exception(
                "service_component_close_failed component=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


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


def create_chat_service(
    config: AppConfig,
    memory_provider: MemoryProvider | None = None,
) -> ChatService:
    synthesizer = create_synthesizer(config)
    if config.llm_backend == "fake":
        return ChatService(
            FakeLLM(),
            synthesizer,
            memory_provider,
            memory_search_limit=config.memory_search_limit,
            memory_search_relativity=config.memory_search_relativity,
            memory_result_max_chars=config.memory_result_max_chars,
            memory_retrieval_mode=config.memory_retrieval_mode,
            memory_preload_timeout_seconds=config.memory_preload_timeout_seconds,
            context_debug=ContextDebugger(enabled=config.context_debug),
        )

    if not config.llm_api_key or not config.llm_model:
        raise RuntimeError("OpenAI-compatible LLM configuration is incomplete")
    return ChatService(
        OpenAICompatibleChatModel(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            model=config.llm_model,
            system_prompt=conversation_system_prompt(config.llm_system_prompt),
            timeout_seconds=config.llm_timeout_seconds,
            input_token_budget=config.context_input_tokens,
            thinking=config.llm_thinking,
        ),
        synthesizer,
        memory_provider,
        memory_search_limit=config.memory_search_limit,
        memory_search_relativity=config.memory_search_relativity,
        memory_result_max_chars=config.memory_result_max_chars,
        memory_retrieval_mode=config.memory_retrieval_mode,
        memory_preload_timeout_seconds=config.memory_preload_timeout_seconds,
        context_debug=ContextDebugger(enabled=config.context_debug),
    )


def create_identity_service(config: AppConfig) -> IdentityService:
    return IdentityService(SqlAlchemyIdentityStore(config.database_url))


def create_voiceprint_client(config: AppConfig) -> VoicePrintClient:
    if not config.voiceprint_url:
        return DisabledVoicePrintClient()
    if not config.voiceprint_api_token:
        raise RuntimeError("VoicePrint configuration is incomplete")
    return HttpVoicePrintClient(
        base_url=config.voiceprint_url,
        api_token=config.voiceprint_api_token,
        timeout_seconds=config.voiceprint_timeout_seconds,
    )


def create_memory_provider(config: AppConfig) -> MemoryProvider:
    if config.memory_backend == "disabled":
        return DisabledMemoryProvider()
    if not config.memos_api_key or not config.memos_profile_template_id:
        raise RuntimeError("MemOS Profile configuration is incomplete")
    return MemosMemoryProvider(
        base_url=config.memos_base_url,
        api_key=config.memos_api_key,
        profile_template_id=config.memos_profile_template_id,
        timeout_seconds=config.memos_timeout_seconds,
    )


def create_memory_writer(
    config: AppConfig,
    provider: MemoryProvider,
) -> MemoryWriter:
    if config.memory_backend == "disabled" or not provider.enabled:
        return DisabledMemoryWriteService()
    return MemoryWriteService(
        SqlAlchemyMemoryJobStore(config.database_url),
        provider,
        poll_seconds=config.memory_job_poll_seconds,
        max_attempts=config.memory_job_max_attempts,
    )


def create_app(
    config: AppConfig | None = None,
    *,
    web_root: Path | None = None,
    chat_service: ChatService | None = None,
    vad: VoiceActivityDetector | None = None,
    recognizer: SpeechRecognizer | None = None,
    identity_service: IdentityService | None = None,
    voiceprint_client: VoicePrintClient | None = None,
    memory_provider: MemoryProvider | None = None,
    memory_writer: MemoryWriter | None = None,
    identity_deletion_service: IdentityDeletionScheduler | None = None,
    dialogue_store: DialogueStore | None = None,
    recovery_rate_limiter: RecoveryRateLimiter | None = None,
    persistence_backend: Literal["postgresql", "memory"] = "postgresql",
    music_service: MusicService | None = None,
    podcast_service: PodcastService | None = None,
) -> FastAPI:
    config = config or load_config()
    if persistence_backend not in {"postgresql", "memory"}:
        raise ValueError("Persistence backend must be postgresql or memory")
    if web_root is not None:
        config = replace(config, web_root=web_root)
    if not config.web_root.is_dir():
        raise RuntimeError(f"Web root does not exist: {config.web_root}")
    resolved_memory_provider = memory_provider or create_memory_provider(config)
    resolved_chat_service = chat_service or create_chat_service(
        config, resolved_memory_provider
    )
    resolved_reminder_service = ReminderService(
        InMemoryReminderStore() if persistence_backend == "memory"
        else SqlAlchemyReminderStore(config.database_url)
    )
    resolved_reminder_audio = ReminderAudio(create_synthesizer(config))
    resolved_music_service = music_service or MusicService(YaohuMusicProvider(config.music_api_key))
    resolved_podcast_service = podcast_service or PodcastService(YaohuPodcastProvider(config.podcast_api_key or config.music_api_key))
    resolved_chat_service.tool_registry = resolved_chat_service.tool_registry.with_tool(MusicTool(resolved_music_service))
    resolved_chat_service.tool_registry = resolved_chat_service.tool_registry.with_tool(PodcastSearchTool(resolved_podcast_service))
    resolved_chat_service.tool_registry = resolved_chat_service.tool_registry.with_tool(PodcastControlTool(resolved_podcast_service))
    resolved_chat_service.tool_registry = resolved_chat_service.tool_registry.with_tool(
        ReminderTool(resolved_reminder_service)
    )
    resolved_vad = vad or create_vad(config)
    resolved_recognizer = recognizer or create_recognizer(config)
    resolved_identity_service = identity_service or create_identity_service(config)
    resolved_dialogue_store = dialogue_store or (
        InMemoryDialogueStore()
        if persistence_backend == "memory"
        else SqlAlchemyDialogueStore(config.database_url)
    )
    resolved_voiceprint_client = voiceprint_client or create_voiceprint_client(config)
    resolved_recovery_rate_limiter = recovery_rate_limiter or (
        InMemoryRecoveryRateLimiter(
            max_attempts=config.recovery_max_attempts,
            window_seconds=config.recovery_window_seconds,
        )
        if persistence_backend == "memory"
        else SqlAlchemyRecoveryRateLimiter(
            config.database_url,
            max_attempts=config.recovery_max_attempts,
            window_seconds=config.recovery_window_seconds,
        )
    )
    profile_cache_coordinator = ProfileCacheCoordinator()
    dialogue_cache_coordinator = DialogueCacheCoordinator()
    resolved_memory_writer = memory_writer or create_memory_writer(
        config, resolved_memory_provider
    )
    resolved_identity_deletion_service = identity_deletion_service
    if resolved_identity_deletion_service is None:
        memory_job_store = (
            resolved_memory_writer.job_store
            if isinstance(resolved_memory_writer, MemoryWriteService) else None
        )
        deletion_store = (
            InMemoryIdentityDeletionStore(
                resolved_identity_service,
                memory_jobs=memory_job_store if isinstance(memory_job_store, InMemoryMemoryJobStore) else None,
            )
            if persistence_backend == "memory"
            else SqlAlchemyIdentityDeletionStore(config.database_url)
        )
        resolved_identity_deletion_service = IdentityDeletionService(
            deletion_store,
            resolved_voiceprint_client,
            resolved_memory_provider,
            profile_cache_coordinator,
            dialogue_store=resolved_dialogue_store,
            dialogue_coordinator=dialogue_cache_coordinator,
            poll_seconds=config.memory_job_poll_seconds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await resolved_identity_service.start()
            await resolved_dialogue_store.start()
            await resolved_recovery_rate_limiter.start()
            await resolved_memory_writer.start()
            await resolved_identity_deletion_service.start()
            await resolved_reminder_service.start()
            logger.info(
                "service_started host=%s port=%s web_root=%s llm_backend=%s llm_model=%s llm_thinking=%s tts_backend=%s asr_backend=%s memory_backend=%s memory_retrieval_mode=%s identity_service=%s",
                config.host,
                config.port,
                config.web_root,
                config.llm_backend,
                config.llm_model or "fake",
                config.llm_thinking,
                config.tts_backend,
                config.asr_backend,
                config.memory_backend,
                config.memory_retrieval_mode,
                type(resolved_identity_service).__name__,
            )
            yield
        finally:
            try:
                await _close_lifecycle_components(
                    ("reminders", resolved_reminder_service.close),
                    ("reminder_audio", resolved_reminder_audio.close),
                    ("music", resolved_music_service.close),
                    ("podcasts", resolved_podcast_service.close),
                    ("identity_deletion", resolved_identity_deletion_service.close),
                    ("memory_writer", resolved_memory_writer.close),
                    ("recovery_rate_limiter", resolved_recovery_rate_limiter.close),
                    ("dialogue_store", resolved_dialogue_store.close),
                    ("chat_service", resolved_chat_service.aclose),
                    ("recognizer", resolved_recognizer.aclose),
                    ("identity_service", resolved_identity_service.close),
                    ("voiceprint_client", resolved_voiceprint_client.aclose),
                    ("memory_provider", resolved_memory_provider.aclose),
                )
            finally:
                logger.info("service_stopped")

    app = FastAPI(title="Newtalk", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.chat_service = resolved_chat_service
    app.state.vad = resolved_vad
    app.state.recognizer = resolved_recognizer
    app.state.identity_service = resolved_identity_service
    app.state.dialogue_store = resolved_dialogue_store
    app.state.voiceprint_client = resolved_voiceprint_client
    app.state.memory_provider = resolved_memory_provider
    app.state.memory_writer = resolved_memory_writer
    app.state.profile_cache_coordinator = profile_cache_coordinator
    app.state.dialogue_cache_coordinator = dialogue_cache_coordinator
    app.state.identity_deletion_service = resolved_identity_deletion_service
    app.state.recovery_rate_limiter = resolved_recovery_rate_limiter
    app.state.reminder_service = resolved_reminder_service
    app.state.reminder_audio = resolved_reminder_audio
    app.state.music_service = resolved_music_service
    app.state.podcast_service = resolved_podcast_service

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
    app.include_router(voiceprint_router)
    app.include_router(memory_router)
    app.include_router(context_debug_router)
    app.include_router(reminder_router)
    app.include_router(music_router)
    app.include_router(podcast_router)
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
)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level.lower(),
    )
