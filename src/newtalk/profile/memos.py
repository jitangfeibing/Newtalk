from typing import Any

import httpx

from newtalk.memory.models import (
    MemoryItem,
    MemoryPage,
    MemoryRecord,
    MemorySearchResult,
    MemoryWriteReceipt,
)
from newtalk.memory.models import MemoryNotFoundError
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

    async def search(
        self,
        scope: ProfileScope,
        *,
        query: str,
        conversation_id: str,
        limit: int,
        relativity: float,
    ) -> MemorySearchResult:
        payload = await self._post(
            "/search/memory",
            {
                "user_id": scope.memos_user_id,
                "conversation_id": conversation_id,
                "query": query,
                "include_memory_view": [
                    "detail_factual",
                    "preference",
                    "event",
                ],
                "memory_limit_number": limit,
                "relativity": relativity,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProfileProviderError("MemOS search/memory returned invalid data")
        items = _parse_search_items(data)
        items.sort(key=lambda item: item.relativity, reverse=True)
        return MemorySearchResult(tuple(items[:limit]))

    async def add_completed_turn(
        self,
        scope: ProfileScope,
        *,
        conversation_id: str,
        turn_id: str,
        speaker_name: str,
        user_text: str,
        assistant_text: str,
    ) -> MemoryWriteReceipt:
        payload = await self._post(
            "/add/message",
            {
                "user_id": scope.memos_user_id,
                "conversation_id": conversation_id,
                "messages": [
                    {
                        "role": "user",
                        "role_id": scope.identity_id,
                        "role_name": speaker_name,
                        "content": user_text,
                    },
                    {"role": "assistant", "content": assistant_text},
                ],
                "allow_memory_view": [
                    "detail_factual",
                    "preference",
                    "profile",
                    "event",
                ],
                "info": {
                    "device_id": scope.device_id,
                    "identity_id": scope.identity_id,
                    "turn_id": turn_id,
                },
                "async_mode": True,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ProfileProviderError("MemOS did not accept completed Turn")
        task_id = data.get("task_id")
        status = data.get("status", "accepted")
        return MemoryWriteReceipt(
            provider_task_id=task_id if isinstance(task_id, str) else None,
            status=status if isinstance(status, str) else "accepted",
        )

    async def list_memories(
        self,
        scope: ProfileScope,
        *,
        page: int,
        size: int,
        kinds: tuple[str, ...],
    ) -> MemoryPage:
        views = tuple(_MEMORY_VIEWS[kind] for kind in kinds)
        payload = await self._post(
            "/get/memory",
            {
                "user_id": scope.memos_user_id,
                "page": page,
                "size": size,
                "include_memory_view": list(views),
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProfileProviderError("MemOS get/memory returned invalid data")
        items = _parse_memory_records(data, kinds)
        items.sort(key=lambda item: item.updated_at or item.created_at or "", reverse=True)
        return MemoryPage(
            items=tuple(items),
            page=_positive_int(data.get("current"), page),
            size=_positive_int(data.get("size"), size),
            total=_nonnegative_int(data.get("total"), len(items)),
            pages=_nonnegative_int(data.get("pages"), 1 if items else 0),
        )

    async def update_memory(
        self,
        scope: ProfileScope,
        *,
        memory_id: str,
        title: str,
        content: str,
    ) -> None:
        await self._assert_memory_owned(scope, memory_id)
        await self._expect_success(
            "/update/memory",
            {"memory_id": memory_id, "title": title, "content": content},
            "MemOS did not update Memory",
        )

    async def delete_memory(self, scope: ProfileScope, *, memory_id: str) -> None:
        await self._assert_memory_owned(scope, memory_id)
        await self._expect_success(
            "/delete/memory",
            {"memory_ids": [memory_id]},
            "MemOS did not delete Memory",
        )

    async def update_profile(
        self,
        scope: ProfileScope,
        *,
        path: str,
        value: str | None,
        locked: bool,
        remove: bool,
    ) -> ProfileSnapshot:
        body: dict[str, Any] = {
            "user_id": scope.memos_user_id,
            "profile_template_id": self.profile_template_id,
        }
        if remove:
            body["remove_fields"] = [path]
        else:
            if value is None:
                raise ValueError("Profile value is required")
            body["metadata"] = _profile_metadata(
                path,
                value=value,
                algorithm_updatable=not locked,
            )
        await self._expect_success(
            "/edit/profile",
            body,
            "MemOS did not update Profile",
        )
        snapshot = await self._load_profile(scope)
        if snapshot is None:
            raise ProfileProviderError("MemOS Profile disappeared after update")
        return snapshot

    async def delete_all_memories(self, scope: ProfileScope) -> None:
        await self._expect_success(
            "/delete/memory",
            {"user_id": scope.memos_user_id},
            "MemOS did not delete member memories",
        )

    async def delete_profile(self, scope: ProfileScope) -> None:
        await self._expect_success(
            "/delete/profile",
            {
                "user_id": scope.memos_user_id,
                "profile_template_id": self.profile_template_id,
            },
            "MemOS did not delete member Profile",
        )

    async def _assert_memory_owned(
        self,
        scope: ProfileScope,
        memory_id: str,
    ) -> None:
        page = 1
        while True:
            memories = await self.list_memories(
                scope,
                page=page,
                size=50,
                kinds=("fact", "preference", "event"),
            )
            if any(item.memory_id == memory_id for item in memories.items):
                return
            if page >= memories.pages:
                break
            page += 1
        raise MemoryNotFoundError("Memory does not belong to this member")

    async def _expect_success(
        self,
        path: str,
        body: dict[str, Any],
        error: str,
    ) -> None:
        payload = await self._post(path, body)
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ProfileProviderError(error)

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


def _parse_search_items(data: dict[str, Any]) -> list[MemoryItem]:
    items: list[MemoryItem] = []
    definitions = (
        ("memory_detail_list", "fact", "memory_key", "memory_value"),
        ("preference_detail_list", "preference", "preference_type", "preference"),
        ("event_detail_list", "event", "event_key", "event_value"),
    )
    for list_name, kind, title_name, content_name in definitions:
        raw_items = data.get(list_name, [])
        if not isinstance(raw_items, list):
            raise ProfileProviderError(f"MemOS {list_name} must be a list")
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            memory_id = raw.get("id")
            content = raw.get(content_name)
            if not isinstance(memory_id, str) or not isinstance(content, str):
                continue
            content = content.strip()
            if not content:
                continue
            title = raw.get(title_name)
            raw_relativity = raw.get("relativity", 0)
            relativity = (
                float(raw_relativity)
                if isinstance(raw_relativity, (int, float))
                else 0.0
            )
            items.append(
                MemoryItem(
                    memory_id=memory_id,
                    kind=kind,
                    title=title.strip() if isinstance(title, str) else "",
                    content=content,
                    relativity=relativity,
                )
            )
    return items


_MEMORY_VIEWS = {
    "fact": "detail_factual",
    "preference": "preference",
    "event": "event",
}


def _parse_memory_records(
    data: dict[str, Any],
    kinds: tuple[str, ...],
) -> list[MemoryRecord]:
    definitions = {
        "fact": ("memory_detail_list", "memory_key", "memory_value"),
        "preference": (
            "preference_detail_list",
            "preference_type",
            "preference",
        ),
        "event": ("event_detail_list", "event_key", "event_value"),
    }
    records: list[MemoryRecord] = []
    for kind in kinds:
        list_name, title_name, content_name = definitions[kind]
        raw_items = data.get(list_name, [])
        if not isinstance(raw_items, list):
            raise ProfileProviderError(f"MemOS {list_name} must be a list")
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            memory_id = raw.get("id")
            content = raw.get(content_name)
            if not isinstance(memory_id, str) or not isinstance(content, str):
                continue
            title = raw.get(title_name)
            records.append(
                MemoryRecord(
                    memory_id=memory_id,
                    kind=kind,
                    title=title.strip() if isinstance(title, str) else "",
                    content=content.strip(),
                    created_at=_optional_string(raw.get("create_time")),
                    updated_at=_optional_string(raw.get("update_time")),
                )
            )
    return records


def _profile_metadata(
    path: str,
    *,
    value: str,
    algorithm_updatable: bool,
) -> dict[str, Any]:
    parts = [part.strip() for part in path.split(".") if part.strip()]
    if not parts:
        raise ValueError("Profile path is required")
    leaf: dict[str, Any] = {
        "value": value,
        "algorithm_updatable": algorithm_updatable,
    }
    result: dict[str, Any] = {parts[-1]: leaf}
    for part in reversed(parts[:-1]):
        result = {part: result}
    return result


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and value > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and value >= 0 else default
