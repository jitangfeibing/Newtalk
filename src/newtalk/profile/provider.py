from typing import Protocol

from newtalk.profile.models import ProfileScope, ProfileSnapshot


class ProfileProviderError(RuntimeError):
    pass


class ProfileProviderUnavailableError(ProfileProviderError):
    pass


class ProfileProvider(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot: ...

    async def aclose(self) -> None: ...


class DisabledProfileProvider:
    @property
    def enabled(self) -> bool:
        return False

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot:
        raise ProfileProviderUnavailableError("Profile provider is disabled")

    async def aclose(self) -> None:
        return None
