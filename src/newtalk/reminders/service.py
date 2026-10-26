import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from newtalk.reminders.models import Reminder, claim
from newtalk.reminders.store import ReminderStore

logger = logging.getLogger(__name__)
Sender = Callable[[dict], Awaitable[None]]


class ReminderService:
    def __init__(self, store: ReminderStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._connections: dict[str, dict[str, Sender]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._lifespans = 0

    async def start(self) -> None:
        if self._lifespans:
            self._lifespans += 1
            return
        await self.store.start()
        await self.store.expire(self.clock(), recover=True)
        self._stopped = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._worker = asyncio.create_task(self._run())
        self._lifespans = 1

    async def close(self) -> None:
        # 同一应用的嵌套 lifespan 共用调度；最后一个使用方负责回收拥有的任务。
        if self._lifespans > 1:
            self._lifespans -= 1
            return
        self._lifespans = 0
        self._stopped.set()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self._connections.clear()
        try:
            await self.store.expire(self.clock(), recover=True)
        finally:
            await self.store.close()

    def connect(self, device_id: str, connection_id: str, send: Sender) -> None:
        self._connections.setdefault(device_id, {})[connection_id] = send

    async def disconnect(self, device_id: str, connection_id: str) -> None:
        connections = self._connections.get(device_id, {})
        connections.pop(connection_id, None)
        if not connections:
            self._connections.pop(device_id, None)
        await self.store.expire(self.clock(), connection_id=connection_id)

    def _validate(self, content: str, due_at: datetime) -> tuple[str, datetime]:
        content = content.strip()
        if not content or len(content) > 160:
            raise ValueError("提醒内容需要 1～160 个字符")
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError("提醒时间必须包含时区")
        due_at = due_at.astimezone(UTC)
        if not self.clock() < due_at <= self.clock() + timedelta(days=366):
            raise ValueError("请选择未来一年内的提醒时间")
        return content, due_at

    async def create(self, device_id: str, content: str, due_at: datetime, request_key: str) -> Reminder:
        content, due_at = self._validate(content, due_at)
        if not request_key or len(request_key) > 128:
            raise ValueError("提醒请求标识无效")
        item = await self.store.create(Reminder(str(uuid4()), device_id, content, due_at, self.clock(), request_key))
        if item.content != content or item.due_at != due_at:
            raise ValueError("同一请求标识已用于另一条提醒")
        await self._notify(device_id, {"type": "reminders_changed"})
        return item

    async def list(self, device_id: str) -> tuple[Reminder, ...]:
        return await self.store.list(device_id)

    async def get(self, device_id: str, reminder_id: str) -> Reminder:
        item = await self.store.get(device_id, reminder_id)
        if item is None:
            raise LookupError("提醒不存在")
        return item

    async def update(self, device_id: str, reminder_id: str, content: str, due_at: datetime) -> Reminder:
        content, due_at = self._validate(content, due_at)
        await self.get(device_id, reminder_id)
        item = await self.store.change(device_id, reminder_id, {"status": ("scheduled",)},
                                       {"content": content, "due_at": due_at})
        if item is None:
            raise ValueError("仅可修改尚未到期的提醒")
        await self._notify(device_id, {"type": "reminders_changed"})
        return item

    async def cancel(self, device_id: str, reminder_id: str) -> Reminder:
        await self.get(device_id, reminder_id)
        item = await self.store.change(device_id, reminder_id, {}, {"status": "cancelled"})
        assert item is not None
        await self._notify(device_id, {"type": "reminder_cancelled", "reminder_id": reminder_id})
        return item

    async def replay(self, device_id: str, reminder_id: str) -> Reminder:
        item = await self.get(device_id, reminder_id)
        if item.status == "cancelled" or item.status == "scheduled" or item.delivery_status in {"pending", "playing"}:
            raise ValueError("这条提醒当前不能重新播放")
        pages = self._connections.get(device_id, {})
        if not pages:
            raise ValueError("请先连接对话，再播放提醒")
        replacement = claim(item, self.clock(), {device_id: next(reversed(pages))})
        expected = {"status": (item.status,)}
        if item.delivery_status is not None:
            expected["delivery_status"] = (item.delivery_status,)
        updated = await self.store.change(device_id, reminder_id, expected, {
            "status": "triggered", "delivery_id": replacement.delivery_id,
            "delivery_status": "pending", "connection_id": replacement.connection_id,
            "delivery_deadline": replacement.delivery_deadline,
        })
        if updated is None:
            raise ValueError("提醒状态已变化，请刷新列表")
        sent = await self._notify(device_id, {"type": "reminder_due", "reminder": updated.payload()},
                                  connection_id=updated.connection_id)
        if not sent:
            await self.store.expire(self.clock(), connection_id=updated.connection_id)
        return updated

    async def ack(self, device_id: str, connection_id: str, reminder_id: str,
                  delivery_id: str, status: str) -> bool:
        if status not in {"waiting", "playing", "completed", "interrupted", "failed", "expired"}:
            return False
        previous = ("pending", "playing") if status in {"interrupted", "failed"} else (
            ("pending",) if status in {"waiting", "playing", "expired"} else ("playing",)
        )
        changes = {"delivery_status": "pending" if status == "waiting" else status}
        if status in {"waiting", "playing"}:
            changes["delivery_deadline"] = self.clock() + timedelta(seconds=90)
        item = await self.store.change(device_id, reminder_id, {
            "status": ("triggered",), "connection_id": (connection_id,),
            "delivery_id": (delivery_id,), "delivery_status": previous,
        }, changes)
        if item is not None:
            if status == "waiting":
                return True
            logger.info("reminder_playback reminder_id=%s delivery_id=%s status=%s",
                        reminder_id, delivery_id, status)
            if status != "playing":
                await self._delivery_ended(item)
            await self._notify(device_id, {"type": "reminders_changed"})
            return True
        return False

    async def tick(self, now: datetime | None = None) -> None:
        async with self._tick_lock:
            now = now or self.clock()
            for expired in await self.store.expire(now):
                await self._delivery_ended(expired)
            connections = {device: next(reversed(pages)) for device, pages in self._connections.items() if pages}
            for item in await self.store.claim_due(now, connections):
                if item.status == "triggered":
                    sent = await self._notify(item.device_id, {
                        "type": "reminder_due", "reminder": item.payload(),
                    }, connection_id=item.connection_id)
                    if not sent:
                        await self.store.expire(now, connection_id=item.connection_id)
                else:
                    await self._notify(item.device_id, {"type": "reminders_changed"})

    async def _delivery_ended(self, item: Reminder) -> None:
        await self._notify(item.device_id, {
            "type": "reminder_delivery_ended", "reminder_id": item.reminder_id,
            "delivery_id": item.delivery_id, "status": item.delivery_status,
        }, connection_id=item.connection_id)

    async def _notify(self, device_id: str, event: dict, *, connection_id: str | None = None) -> bool:
        sent = False
        for key, sender in tuple(self._connections.get(device_id, {}).items()):
            if connection_id is not None and key != connection_id:
                continue
            try:
                async with asyncio.timeout(1):
                    await sender(event)
                sent = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reminder_notification_failed connection_id=%s", key)
        return sent

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reminder_poll_failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=1)
            except TimeoutError:
                pass
