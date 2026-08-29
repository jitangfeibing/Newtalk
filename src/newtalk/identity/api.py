from collections import defaultdict, deque
from datetime import datetime
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from newtalk.config import AppConfig
from newtalk.identity.models import Device, Identity
from newtalk.identity.service import (
    DeviceAuthenticationError,
    DeviceRecoveryError,
    IdentityNotFoundError,
    IdentityService,
)


router = APIRouter(prefix="/api", tags=["identity"])


class RecoveryRateLimiter:
    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        if len(self._attempts) > 1024:
            cutoff = now - self._window_seconds
            self._attempts = defaultdict(
                deque,
                {
                    candidate: attempts
                    for candidate, attempts in self._attempts.items()
                    if attempts and attempts[-1] > cutoff
                },
            )
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - self._window_seconds:
            attempts.popleft()
        if len(attempts) >= self._max_attempts:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many recovery attempts",
            )
        attempts.append(now)

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)


class DeviceResponse(BaseModel):
    device_id: str
    created_at: datetime
    recovery_code: str | None = None


class RecoveryRequest(BaseModel):
    recovery_code: str = Field(min_length=10, max_length=64)


class IdentityCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    nickname: str | None = Field(default=None, max_length=80)
    relationship: str | None = Field(default=None, max_length=80)
    avatar: str | None = Field(default=None, max_length=500)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value

    @field_validator("nickname", "relationship", "avatar")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class IdentityUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    nickname: str | None = Field(default=None, max_length=80)
    relationship: str | None = Field(default=None, max_length=80)
    avatar: str | None = Field(default=None, max_length=500)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("display_name must not be null")
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value

    @field_validator("nickname", "relationship", "avatar")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class IdentityResponse(BaseModel):
    identity_id: str
    display_name: str
    nickname: str | None
    relationship: str | None
    avatar: str | None
    status: str
    created_at: datetime
    updated_at: datetime


def _service(request: Request) -> IdentityService:
    return request.app.state.identity_service


def _config(request: Request) -> AppConfig:
    return request.app.state.config


def _device_response(device: Device, recovery_code: str | None = None) -> DeviceResponse:
    return DeviceResponse(
        device_id=device.device_id,
        created_at=device.created_at,
        recovery_code=recovery_code,
    )


def _identity_response(identity: Identity) -> IdentityResponse:
    return IdentityResponse(
        identity_id=identity.identity_id,
        display_name=identity.display_name,
        nickname=identity.nickname,
        relationship=identity.relationship,
        avatar=identity.avatar,
        status=identity.status.value,
        created_at=identity.created_at,
        updated_at=identity.updated_at,
    )


def _set_device_cookie(response: Response, config: AppConfig, credential: str) -> None:
    response.set_cookie(
        key=config.device_cookie_name,
        value=credential,
        max_age=config.device_cookie_max_age_days * 24 * 60 * 60,
        httponly=True,
        secure=config.device_cookie_secure,
        samesite="lax",
        path="/",
    )


async def require_device(
    request: Request,
    service: IdentityService = Depends(_service),
    config: AppConfig = Depends(_config),
) -> Device:
    try:
        return await service.authenticate_device(
            request.cookies.get(config.device_cookie_name)
        )
    except DeviceAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device registration is required",
        ) from exc


@router.get("/device", response_model=DeviceResponse)
async def get_device(device: Device = Depends(require_device)) -> DeviceResponse:
    return _device_response(device)


@router.post("/device", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(
    request: Request,
    response: Response,
    service: IdentityService = Depends(_service),
    config: AppConfig = Depends(_config),
) -> DeviceResponse:
    credential = request.cookies.get(config.device_cookie_name)
    if credential:
        try:
            device = await service.authenticate_device(credential)
        except DeviceAuthenticationError:
            pass
        else:
            response.status_code = status.HTTP_200_OK
            return _device_response(device)

    registration = await service.register_device()
    _set_device_cookie(response, config, registration.credential)
    return _device_response(registration.device, registration.recovery_code)


@router.post("/device/recover", response_model=DeviceResponse)
async def recover_device(
    payload: RecoveryRequest,
    request: Request,
    response: Response,
    service: IdentityService = Depends(_service),
    config: AppConfig = Depends(_config),
) -> DeviceResponse:
    client_key = request.client.host if request.client else "unknown"
    limiter: RecoveryRateLimiter = request.app.state.recovery_rate_limiter
    limiter.check(client_key)
    try:
        registration = await service.recover_device(payload.recovery_code)
    except DeviceRecoveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recovery code is invalid",
        ) from exc
    limiter.clear(client_key)
    _set_device_cookie(response, config, registration.credential)
    return _device_response(registration.device)


@router.post("/device/recovery-code", response_model=DeviceResponse)
async def rotate_recovery_code(
    response: Response,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_service),
) -> DeviceResponse:
    recovery_code = await service.rotate_recovery_code(device.device_id)
    return _device_response(device, recovery_code)


@router.get("/members", response_model=list[IdentityResponse])
async def list_members(
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_service),
) -> list[IdentityResponse]:
    identities = await service.list_identities(device.device_id)
    return [_identity_response(identity) for identity in identities]


@router.post(
    "/members",
    response_model=IdentityResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_member(
    payload: IdentityCreateRequest,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_service),
) -> IdentityResponse:
    identity = await service.create_identity(
        device_id=device.device_id,
        display_name=payload.display_name,
        nickname=payload.nickname,
        relationship=payload.relationship,
        avatar=payload.avatar,
    )
    return _identity_response(identity)


@router.patch("/members/{identity_id}", response_model=IdentityResponse)
async def update_member(
    identity_id: str,
    payload: IdentityUpdateRequest,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_service),
) -> IdentityResponse:
    changes = {
        field: getattr(payload, field)
        for field in payload.model_fields_set
        if field in {"display_name", "nickname", "relationship", "avatar"}
    }
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one member field is required",
        )
    try:
        identity = await service.update_identity(
            device_id=device.device_id,
            identity_id=identity_id,
            changes=changes,
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return _identity_response(identity)


@router.delete("/members/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(
    identity_id: str,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_service),
) -> Response:
    try:
        await service.delete_identity(
            device_id=device.device_id,
            identity_id=identity_id,
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
