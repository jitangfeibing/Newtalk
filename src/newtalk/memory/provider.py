from typing import Protocol

from newtalk.memory.models import MemoryPage, MemorySearchResult, MemoryWriteReceipt
from newtalk.profile.models import ProfileScope, ProfileSnapshot
from newtalk.profile.provider import ProfileProviderUnavailableError


class MemoryProvider(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot: ...

    async def search(
        self,
        scope: ProfileScope,
        *,
        query: str,
        conversation_id: str,
        limit: int,
        relativity: float,
    ) -> MemorySearchResult: ...

    async def add_completed_turn(
        self,
        scope: ProfileScope,
        *,
        conversation_id: str,
        turn_id: str,
        speaker_name: str,
        user_text: str,
        assistant_text: str,
    ) -> MemoryWriteReceipt: ...

    async def list_memories(
        self,
        scope: ProfileScope,
        *,
        page: int,
        size: int,
        kinds: tuple[str, ...],
    ) -> MemoryPage: ...

    async def update_memory(
        self,
        scope: ProfileScope,
        *,
        memory_id: str,
        title: str,
        content: str,
    ) -> None: ...

    async def delete_memory(self, scope: ProfileScope, *, memory_id: str) -> None: ...

    async def update_profile(
        self,
        scope: ProfileScope,
        *,
        path: str,
        value: str | None,
        locked: bool,
        remove: bool,
    ) -> ProfileSnapshot: ...

    async def delete_all_memories(self, scope: ProfileScope) -> None: ...

    async def delete_profile(self, scope: ProfileScope) -> None: ...

    async def aclose(self) -> None: ...


class DisabledMemoryProvider:
    @property
    def enabled(self) -> bool:
        return False

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def search(
        self,
        scope: ProfileScope,
        *,
        query: str,
        conversation_id: str,
        limit: int,
        relativity: float,
    ) -> MemorySearchResult:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

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
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def list_memories(
        self,
        scope: ProfileScope,
        *,
        page: int,
        size: int,
        kinds: tuple[str, ...],
    ) -> MemoryPage:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def update_memory(
        self,
        scope: ProfileScope,
        *,
        memory_id: str,
        title: str,
        content: str,
    ) -> None:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def delete_memory(self, scope: ProfileScope, *, memory_id: str) -> None:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def update_profile(
        self,
        scope: ProfileScope,
        *,
        path: str,
        value: str | None,
        locked: bool,
        remove: bool,
    ) -> ProfileSnapshot:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def delete_all_memories(self, scope: ProfileScope) -> None:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def delete_profile(self, scope: ProfileScope) -> None:
        raise ProfileProviderUnavailableError("Memory provider is disabled")

    async def aclose(self) -> None:
        return None
