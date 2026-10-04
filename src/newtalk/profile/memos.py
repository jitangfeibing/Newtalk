from typing import Any

import httpx

from newtalk.profile.models import ProfileScope, ProfileSnapshot, parse_profile_fields
from newtalk.profile.provider import (
    ProfileProviderError,
    ProfileProviderUnavailableError,
)


class MemosProfileProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        profile_template_id: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile_template_id = profile_template_id
        self._headers = {"Authorization": f"Token {api_key}"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=self._headers,
            timeout=timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return True

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot:
        snapshot = await self._load_profile(scope)
        if snapshot is not None:
            return snapshot

        try:
            await self._bind_profile(scope)
        except ProfileProviderError:
            # Two sessions may try to bind the same pre-existing member concurrently.
            snapshot = await self._load_profile(scope)
            if snapshot is not None:
                return snapshot
            raise

        snapshot = await self._load_profile(scope)
        if snapshot is not None:
            return snapshot
        return ProfileSnapshot(
            identity_id=scope.identity_id,
            profile_template_id=self.profile_template_id,
            fields=(),
        )

    async def _load_profile(self, scope: ProfileScope) -> ProfileSnapshot | None:
        payload = await self._post(
            "/get/memory",
            {
                "user_id": scope.memos_user_id,
                "include_memory_view": ["profile"],
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProfileProviderError("MemOS get/memory returned invalid data")
        profiles = data.get("profile_detail_list", [])
        if not isinstance(profiles, list):
            raise ProfileProviderError("MemOS profile_detail_list must be a list")
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            if profile.get("profile_template_id") != self.profile_template_id:
                continue
            if profile.get("status") not in {None, "activated"}:
                continue
            return ProfileSnapshot(
                identity_id=scope.identity_id,
                profile_template_id=self.profile_template_id,
                fields=parse_profile_fields(profile.get("properties", {})),
            )
        return None

    async def _bind_profile(self, scope: ProfileScope) -> None:
        payload = await self._post(
            "/bind/profile_template",
            {
                "bind_list": [
                    {
                        "user_id": scope.memos_user_id,
                        "profile_template_id": self.profile_template_id,
                    }
                ]
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ProfileProviderError("MemOS did not confirm Profile binding")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProfileProviderUnavailableError("MemOS is unavailable") from exc
        if response.status_code >= 500:
            raise ProfileProviderUnavailableError(
                f"MemOS request failed ({response.status_code})"
            )
        if not response.is_success:
            raise ProfileProviderError(f"MemOS request failed ({response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProfileProviderError("MemOS returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProfileProviderError("MemOS response must be an object")
        if payload.get("code") != 0:
            message = payload.get("message")
            raise ProfileProviderError(
                message if isinstance(message, str) and message else "MemOS request failed"
            )
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
