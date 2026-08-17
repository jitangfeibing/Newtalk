from contextlib import asynccontextmanager
from dataclasses import replace
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from newtalk import __version__
from newtalk.chat import ChatService, FakeLLM, OpenAICompatibleChatModel
from newtalk.config import AppConfig, load_config
from newtalk.logging_config import configure_logging
from newtalk.transport import websocket_router


logger = logging.getLogger(__name__)


def create_chat_service(config: AppConfig) -> ChatService:
    if config.llm_backend == "fake":
        return ChatService(FakeLLM())

    if not config.llm_api_key or not config.llm_model:
        raise RuntimeError("OpenAI-compatible LLM configuration is incomplete")
    return ChatService(
        OpenAICompatibleChatModel(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            model=config.llm_model,
            system_prompt=config.llm_system_prompt,
            timeout_seconds=config.llm_timeout_seconds,
        )
    )


def create_app(
    config: AppConfig | None = None,
    *,
    web_root: Path | None = None,
    chat_service: ChatService | None = None,
) -> FastAPI:
    config = config or load_config()
    if web_root is not None:
        config = replace(config, web_root=web_root)
    if not config.web_root.is_dir():
        raise RuntimeError(f"Web root does not exist: {config.web_root}")
    resolved_chat_service = chat_service or create_chat_service(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "service_started host=%s port=%s web_root=%s llm_backend=%s llm_model=%s",
            config.host,
            config.port,
            config.web_root,
            config.llm_backend,
            config.llm_model or "fake",
        )
        try:
            yield
        finally:
            await resolved_chat_service.aclose()
            logger.info("service_stopped")

    app = FastAPI(title="Newtalk", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.chat_service = resolved_chat_service

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "newtalk",
            "version": __version__,
        }

    app.include_router(websocket_router)

    app.mount(
        "/",
        StaticFiles(directory=config.web_root, html=True),
        name="web",
    )

    return app


runtime_config = load_config()
configure_logging(runtime_config.log_level)
app = create_app(runtime_config)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level.lower(),
    )
