from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from newtalk.identity.api import require_device
from newtalk.identity.models import Device, IdentityStatus
from newtalk.identity.service import IdentityNotFoundError, IdentityService
from newtalk.memory.models import MemoryPage, MemoryRecord
from newtalk.memory.models import MemoryNotFoundError
from newtalk.memory.provider import MemoryProvider
from newtalk.profile.models import ProfileScope, ProfileSnapshot
from newtalk.profile.provider import (
    ProfileProviderError,
    ProfileProviderUnavailableError,
)
from newtalk.profile.session import ProfileCacheCoordinator


router = APIRouter(prefix="/api/members", tags=["memory"])
MemoryKindFilter = Literal["all", "fact", "preference", "event"]


class ProfileFieldResponse(BaseModel):
    path: str
    value: str
    locked: bool


class ProfileResponse(BaseModel):
    enabled: bool
    identity_id: str
    profile_template_id: str | None
    fields: list[ProfileFieldResponse]


class ProfileUpdateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    value: str | None = Field(default=None, max_length=2000)
    locked: bool = False
    remove: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(".")]
        if not parts or any(not part for part in parts) or len(parts) > 8:
            raise ValueError("Profile path must contain 1 to 8 named segments")
        return ".".join(parts)

    @model_validator(mode="after")
    def validate_operation(self) -> "ProfileUpdateRequest":
        if not self.remove:
            self.value = (self.value or "").strip()
            if not self.value:
                raise ValueError("Profile value is required")
        return self


class MemoryRecordResponse(BaseModel):
    memory_id: str
    kind: str
    title: str
    content: str
    created_at: str | None
    updated_at: str | None


class MemoryPageResponse(BaseModel):
    items: list[MemoryRecordResponse]
    page: int
    size: int
    total: int
    pages: int


class MemoryUpdateRequest(BaseModel):
    title: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=8000)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Memory content must not be blank")
        return value


def _identity_service(request: Request) -> IdentityService:
    return request.app.state.identity_service


def _provider(request: Request) -> MemoryProvider:
    return request.app.state.profile_provider


def _coordinator(request: Request) -> ProfileCacheCoordinator:
    return request.app.state.profile_cache_coordinator


async def _scope(
    identity_id: str,
    device: Device,
    service: IdentityService,
) -> ProfileScope:
    try:
        identity = await service.get_identity(
            device_id=device.device_id,
            identity_id=identity_id,
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Member not found") from exc
    if identity.status is not IdentityStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="Member not found")
    return ProfileScope(device.device_id, identity.identity_id)


def _ensure_enabled(provider: MemoryProvider) -> None:
    if not provider.enabled:
        raise HTTPException(status_code=503, detail="Memory is disabled")


def _provider_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(status_code=404, detail="Memory not found")
    if isinstance(exc, ProfileProviderUnavailableError):
        return HTTPException(status_code=503, detail="Memory provider is unavailable")
    return HTTPException(status_code=502, detail=str(exc) or "Memory provider failed")


def _profile_response(snapshot: ProfileSnapshot) -> ProfileResponse:
    return ProfileResponse(
        enabled=True,
        identity_id=snapshot.identity_id,
        profile_template_id=snapshot.profile_template_id,
        fields=[
            ProfileFieldResponse(
                path=field.path,
                value=field.value,
                locked=field.algorithm_updatable is False,
            )
            for field in snapshot.fields
        ],
    )


def _memory_response(page: MemoryPage) -> MemoryPageResponse:
    return MemoryPageResponse(
        items=[
            MemoryRecordResponse(
                memory_id=record.memory_id,
                kind=record.kind,
                title=record.title,
                content=record.content,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in page.items
        ],
        page=page.page,
        size=page.size,
        total=page.total,
        pages=page.pages,
    )


@router.get("/{identity_id}/profile", response_model=ProfileResponse)
async def get_profile(
    identity_id: str,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_identity_service),
    provider: MemoryProvider = Depends(_provider),
) -> ProfileResponse:
    _ensure_enabled(provider)
    scope = await _scope(identity_id, device, service)
    try:
        return _profile_response(await provider.prepare_profile(scope))
    except (ProfileProviderError, MemoryNotFoundError) as exc:
        raise _provider_error(exc) from exc


@router.patch("/{identity_id}/profile", response_model=ProfileResponse)
async def update_profile(
    identity_id: str,
    payload: ProfileUpdateRequest,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_identity_service),
    provider: MemoryProvider = Depends(_provider),
    coordinator: ProfileCacheCoordinator = Depends(_coordinator),
) -> ProfileResponse:
    _ensure_enabled(provider)
    scope = await _scope(identity_id, device, service)
    try:
        snapshot = await provider.update_profile(
            scope,
            path=payload.path,
            value=payload.value,
            locked=payload.locked,
            remove=payload.remove,
        )
    except (ProfileProviderError, ValueError) as exc:
        raise _provider_error(exc) from exc
    coordinator.update(device.device_id, snapshot)
    return _profile_response(snapshot)


@router.get("/{identity_id}/memories", response_model=MemoryPageResponse)
async def list_memories(
    identity_id: str,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=50),
    kind: MemoryKindFilter = "all",
    query: str | None = Query(default=None, max_length=500),
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_identity_service),
    provider: MemoryProvider = Depends(_provider),
) -> MemoryPageResponse:
    _ensure_enabled(provider)
    scope = await _scope(identity_id, device, service)
    kinds = ("fact", "preference", "event") if kind == "all" else (kind,)
    try:
        if query and query.strip():
            if page != 1:
                raise HTTPException(status_code=422, detail="Search only supports page 1")
            result = await provider.search(
                scope,
                query=query.strip(),
                conversation_id="memory-center",
                limit=min(size, 25),
                relativity=0.0,
            )
            records = tuple(
                MemoryRecord(
                    memory_id=item.memory_id,
                    kind=item.kind,
                    title=item.title,
                    content=item.content,
                )
                for item in result.items
                if item.kind in kinds
            )
            memory_page = MemoryPage(
                items=records,
                page=1,
                size=size,
                total=len(records),
                pages=1 if records else 0,
            )
        else:
            memory_page = await provider.list_memories(
                scope,
                page=page,
                size=size,
                kinds=kinds,
            )
    except HTTPException:
        raise
    except (ProfileProviderError, MemoryNotFoundError) as exc:
        raise _provider_error(exc) from exc
    return _memory_response(memory_page)


@router.patch("/{identity_id}/memories/{memory_id}", status_code=204)
async def update_memory(
    identity_id: str,
    memory_id: str,
    payload: MemoryUpdateRequest,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_identity_service),
    provider: MemoryProvider = Depends(_provider),
) -> Response:
    _ensure_enabled(provider)
    scope = await _scope(identity_id, device, service)
    try:
        await provider.update_memory(
            scope,
            memory_id=memory_id,
            title=payload.title,
            content=payload.content,
        )
    except (ProfileProviderError, MemoryNotFoundError) as exc:
        raise _provider_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{identity_id}/memories/{memory_id}", status_code=204)
async def delete_memory(
    identity_id: str,
    memory_id: str,
    device: Device = Depends(require_device),
    service: IdentityService = Depends(_identity_service),
    provider: MemoryProvider = Depends(_provider),
) -> Response:
    _ensure_enabled(provider)
    scope = await _scope(identity_id, device, service)
    try:
        await provider.delete_memory(scope, memory_id=memory_id)
    except (ProfileProviderError, MemoryNotFoundError) as exc:
        raise _provider_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
