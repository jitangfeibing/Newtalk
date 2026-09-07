from contextlib import asynccontextmanager
import logging
import secrets

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from pydantic import BaseModel

from newtalk_voiceprint import __version__
from newtalk_voiceprint.audio import InvalidAudioError
from newtalk_voiceprint.config import VoicePrintConfig
from newtalk_voiceprint.embedder import CampPlusEmbedder, DeterministicEmbedder
from newtalk_voiceprint.service import IdentityNotFoundError, VoicePrintService
from newtalk_voiceprint.store import VoicePrintStore


logger = logging.getLogger(__name__)


class RegistrationResponse(BaseModel):
    identity_id: str
    model: str
    enrolled_at: str


class IdentificationResponse(BaseModel):
    identity_id: str | None
    score: float
    matched: bool
    elapsed_ms: float


def create_service(config: VoicePrintConfig) -> VoicePrintService:
    embedder = (
        CampPlusEmbedder(model_id=config.model_id, device=config.device)
        if config.backend == "campplus"
        else DeterministicEmbedder()
    )
    return VoicePrintService(
        VoicePrintStore(config.database_url),
        embedder,
        threshold=config.similarity_threshold,
        min_sample_seconds=config.min_sample_seconds,
        max_sample_seconds=config.max_sample_seconds,
        max_audio_bytes=config.max_audio_bytes,
    )


def create_app(
    config: VoicePrintConfig | None = None,
    *,
    service: VoicePrintService | None = None,
) -> FastAPI:
    config = config or VoicePrintConfig.from_environment()
    resolved_service = service or create_service(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await resolved_service.start()
        logger.info("voiceprint_started backend=%s model=%s", config.backend, resolved_service.model_name)
        try:
            yield
        finally:
            await resolved_service.close()
            logger.info("voiceprint_stopped")

    app = FastAPI(title="Newtalk VoicePrint", version=__version__, lifespan=lifespan)
    app.state.service = resolved_service

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {config.api_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")

    @app.get("/health")
    async def health() -> dict[str, str]:
        try:
            await resolved_service.ping()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok", "service": "newtalk-voiceprint", "model": resolved_service.model_name}

    @app.post("/v1/templates", response_model=RegistrationResponse, dependencies=[Depends(authorize)])
    async def register_template(
        device_id: str = Form(...),
        identity_id: str = Form(...),
        samples: list[UploadFile] = File(...),
    ) -> RegistrationResponse:
        if len(samples) != 3:
            raise HTTPException(status_code=422, detail="exactly three voice samples are required")
        try:
            enrolled_at = await resolved_service.register(
                device_id=device_id,
                identity_id=identity_id,
                samples=[
                    await sample.read(config.max_audio_bytes + 1)
                    for sample in samples
                ],
            )
        except InvalidAudioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdentityNotFoundError as exc:
            raise HTTPException(status_code=404, detail="identity not found") from exc
        return RegistrationResponse(
            identity_id=identity_id,
            model=resolved_service.model_name,
            enrolled_at=enrolled_at.isoformat(),
        )

    @app.post("/v1/identify", response_model=IdentificationResponse, dependencies=[Depends(authorize)])
    async def identify(
        device_id: str = Form(...),
        sample: UploadFile = File(...),
    ) -> IdentificationResponse:
        try:
            result = await resolved_service.identify(
                device_id=device_id,
                sample=await sample.read(config.max_audio_bytes + 1),
            )
        except InvalidAudioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return IdentificationResponse(
            identity_id=result.identity_id,
            score=result.score,
            matched=result.matched,
            elapsed_ms=result.elapsed_ms,
        )

    @app.delete("/v1/templates/{identity_id}", dependencies=[Depends(authorize)])
    async def delete_template(identity_id: str, device_id: str) -> dict[str, bool]:
        deleted = await resolved_service.delete(device_id=device_id, identity_id=identity_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="voiceprint template not found")
        return {"deleted": True}

    return app


runtime_config = VoicePrintConfig.from_environment()
logging.basicConfig(level=runtime_config.log_level)
app = create_app(runtime_config)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level.lower(),
    )
