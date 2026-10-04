import asyncio
import logging
from time import perf_counter

from newtalk.identity import IdentityService
from newtalk.profile.models import ProfileScope, ProfileSnapshot
from newtalk.profile.provider import ProfileProvider


logger = logging.getLogger(__name__)


class SessionProfileCache:
    """Non-blocking, per-identity Profile snapshots for one WebSocket session."""

    def __init__(
        self,
        *,
        device_id: str,
        identity_service: IdentityService,
        provider: ProfileProvider,
    ) -> None:
        self.device_id = device_id
        self._identity_service = identity_service
        self._provider = provider
        self._snapshots: dict[str, ProfileSnapshot] = {}
        self._attempted: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._prefetch_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._provider.enabled

    def start(self) -> None:
        if not self.enabled or self._closed or self._prefetch_task is not None:
            return
        self._prefetch_task = asyncio.create_task(self._prefetch_all())

    def snapshot_for(self, identity_id: str) -> ProfileSnapshot | None:
        snapshot = self._snapshots.get(identity_id)
        if snapshot is None:
            self.schedule(identity_id)
        return snapshot

    def schedule(self, identity_id: str) -> None:
        if (
            not self.enabled
            or self._closed
            or identity_id in self._attempted
            or identity_id in self._snapshots
        ):
            return
        self._attempted.add(identity_id)
        task = asyncio.create_task(self._load(identity_id))
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    async def wait_until_idle(self) -> None:
        if self._prefetch_task is not None:
            await asyncio.gather(self._prefetch_task, return_exceptions=True)
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        if self._prefetch_task is not None and not self._prefetch_task.done():
            tasks.append(self._prefetch_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _prefetch_all(self) -> None:
        try:
            identities = await self._identity_service.list_identities(self.device_id)
        except Exception:
            logger.exception("profile_prefetch_list_failed device_id=%s", self.device_id)
            return
        for identity in identities:
            self.schedule(identity.identity_id)

    async def _load(self, identity_id: str) -> None:
        started_at = perf_counter()
        try:
            snapshot = await self._provider.prepare_profile(
                ProfileScope(device_id=self.device_id, identity_id=identity_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "profile_prefetch_failed device_id=%s identity_id=%s error_type=%s error=%s elapsed_ms=%.1f",
                self.device_id,
                identity_id,
                type(exc).__name__,
                str(exc),
                (perf_counter() - started_at) * 1000,
            )
            return
        if not self._closed:
            self._snapshots[identity_id] = snapshot
        logger.info(
            "profile_prefetch_completed device_id=%s identity_id=%s fields=%s elapsed_ms=%.1f",
            self.device_id,
            identity_id,
            len(snapshot.fields),
            (perf_counter() - started_at) * 1000,
        )

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
