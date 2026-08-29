from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel

from newtalk.identity.api import require_device
from newtalk.identity.models import Device
from newtalk.identity.service import IdentityNotFoundError, IdentityService
from newtalk.voiceprint.client import VoicePrintClient, VoicePrintError, VoicePrintUnavailableError


router = APIRouter(prefix="/api/members", tags=["voiceprint"])
MAX_SAMPLE_BYTES = 1_000_000


class VoicePrintEnrollmentResponse(BaseModel):
    identity_id: str
    model: str
    enrolled_at: datetime


def _identity_service(request: Request) -> IdentityService:
    return request.app.state.identity_service


def _voiceprint_client(request: Request) -> VoicePrintClient:
    return request.app.state.voiceprint_client


async def _read_sample(sample: UploadFile) -> bytes:
    payload = await sample.read(MAX_SAMPLE_BYTES + 1)
    if len(payload) > MAX_SAMPLE_BYTES:
        raise HTTPException(status_code=413, detail="Voice sample is too large")
    return payload


@router.post("/{identity_id}/voiceprint", response_model=VoicePrintEnrollmentResponse)
async def register_voiceprint(
    identity_id: str,
    samples: list[UploadFile] = File(...),
    device: Device = Depends(require_device),
    identity_service: IdentityService = Depends(_identity_service),
    voiceprint_client: VoicePrintClient = Depends(_voiceprint_client),
) -> VoicePrintEnrollmentResponse:
    if len(samples) != 3:
        raise HTTPException(status_code=422, detail="Exactly three voice samples are required")
    try:
        await identity_service.get_identity(
            device_id=device.device_id,
            identity_id=identity_id,
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Member not found") from exc
    try:
        enrollment = await voiceprint_client.register(
            device_id=device.device_id,
            identity_id=identity_id,
            samples=[await _read_sample(sample) for sample in samples],
        )
    except VoicePrintUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VoicePrintError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VoicePrintEnrollmentResponse(
        identity_id=enrollment.identity_id,
        model=enrollment.model,
        enrolled_at=enrollment.enrolled_at,
    )


@router.delete("/{identity_id}/voiceprint", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voiceprint(
    identity_id: str,
    device: Device = Depends(require_device),
    identity_service: IdentityService = Depends(_identity_service),
    voiceprint_client: VoicePrintClient = Depends(_voiceprint_client),
) -> Response:
    try:
        await identity_service.get_identity(
            device_id=device.device_id,
            identity_id=identity_id,
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Member not found") from exc
    try:
        await voiceprint_client.delete(
            device_id=device.device_id,
            identity_id=identity_id,
        )
    except VoicePrintUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VoicePrintError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)

