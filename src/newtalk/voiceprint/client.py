from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx


class VoicePrintError(RuntimeError):
    pass


class VoicePrintUnavailableError(VoicePrintError):
    pass


@dataclass(frozen=True, slots=True)
class VoicePrintEnrollment:
    identity_id: str
    model: str
    enrolled_at: datetime


@dataclass(frozen=True, slots=True)
class VoicePrintIdentification:
    identity_id: str | None
    score: float
    matched: bool
    elapsed_ms: float


class VoicePrintClient(Protocol):
    async def register(
        self,
        *,
        device_id: str,
        identity_id: str,
        samples: list[bytes],
    ) -> VoicePrintEnrollment: ...

    async def identify(
        self,
        *,
        device_id: str,
        sample: bytes,
    ) -> VoicePrintIdentification: ...

    async def delete(self, *, device_id: str, identity_id: str) -> None: ...

    async def aclose(self) -> None: ...


class DisabledVoicePrintClient:
    async def register(
        self,
        *,
        device_id: str,
        identity_id: str,
        samples: list[bytes],
    ) -> VoicePrintEnrollment:
        raise VoicePrintUnavailableError("VoicePrint service is disabled")

    async def identify(
        self,
        *,
        device_id: str,
        sample: bytes,
    ) -> VoicePrintIdentification:
        raise VoicePrintUnavailableError("VoicePrint service is disabled")

    async def delete(self, *, device_id: str, identity_id: str) -> None:
        raise VoicePrintUnavailableError("VoicePrint service is disabled")

    async def aclose(self) -> None:
        return None


class HttpVoicePrintClient:
    def __init__(self, *, base_url: str, api_token: str, timeout_seconds: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=timeout_seconds,
        )

    async def register(
        self,
        *,
        device_id: str,
        identity_id: str,
        samples: list[bytes],
    ) -> VoicePrintEnrollment:
        files = [
            ("samples", (f"sample-{index}.wav", sample, "audio/wav"))
            for index, sample in enumerate(samples, start=1)
        ]
        try:
            response = await self._client.post(
                "/v1/templates",
                data={"device_id": device_id, "identity_id": identity_id},
                files=files,
            )
        except httpx.HTTPError as exc:
            raise VoicePrintUnavailableError("VoicePrint service is unavailable") from exc
        self._raise_for_status(response)
        payload = response.json()
        return VoicePrintEnrollment(
            identity_id=payload["identity_id"],
            model=payload["model"],
            enrolled_at=datetime.fromisoformat(payload["enrolled_at"]),
        )

    async def identify(
        self,
        *,
        device_id: str,
        sample: bytes,
    ) -> VoicePrintIdentification:
        try:
            response = await self._client.post(
                "/v1/identify",
                data={"device_id": device_id},
                files={"sample": ("utterance.wav", sample, "audio/wav")},
            )
        except httpx.HTTPError as exc:
            raise VoicePrintUnavailableError("VoicePrint service is unavailable") from exc
        self._raise_for_status(response)
        payload = response.json()
        return VoicePrintIdentification(
            identity_id=payload.get("identity_id"),
            score=float(payload["score"]),
            matched=bool(payload["matched"]),
            elapsed_ms=float(payload["elapsed_ms"]),
        )

    async def delete(self, *, device_id: str, identity_id: str) -> None:
        try:
            response = await self._client.delete(
                f"/v1/templates/{identity_id}",
                params={"device_id": device_id},
            )
        except httpx.HTTPError as exc:
            raise VoicePrintUnavailableError("VoicePrint service is unavailable") from exc
        self._raise_for_status(response)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = None
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            pass
        if response.status_code >= 500:
            raise VoicePrintUnavailableError(detail or "VoicePrint service failed")
        raise VoicePrintError(detail or f"VoicePrint request failed ({response.status_code})")

    async def aclose(self) -> None:
        await self._client.aclose()
