import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from newtalk.reminders.service import ReminderService
from newtalk.reminders.store import InMemoryReminderStore

NOW = datetime(2026, 10, 26, 1, tzinfo=UTC)


def service():
    return ReminderService(InMemoryReminderStore(), clock=lambda: NOW)


def test_reminder_creation_is_durable_idempotent_and_scoped():
    async def scenario():
        reminders = service()
        first = await reminders.create("device-a", "  取快递  ", NOW + timedelta(minutes=10), "request-1")
        again = await reminders.create("device-a", "取快递", first.due_at, "request-1")
        assert first == again
        assert first.content == "取快递"
        assert len(await reminders.list("device-a")) == 1
        assert await reminders.list("device-b") == ()
        with pytest.raises(LookupError):
            await reminders.get("device-b", first.reminder_id)
        with pytest.raises(ValueError):
            await reminders.create("device-a", "", first.due_at, "request-2")
        with pytest.raises(ValueError):
            await reminders.create("device-a", "取快递", NOW, "request-3")
        with pytest.raises(ValueError):
            await reminders.create("device-a", "取快递", datetime(2026, 10, 27), "request-4")
    asyncio.run(scenario())


def test_due_reminder_goes_to_only_one_connected_page_and_ack_is_correlated():
    async def scenario():
        reminders = service()
        first_events, second_events = [], []

        async def first_send(event):
            first_events.append(event)

        async def second_send(event):
            second_events.append(event)

        reminders.connect("device-a", "connection-1", first_send)
        reminders.connect("device-a", "connection-2", second_send)
        item = await reminders.create("device-a", "取快递", NOW + timedelta(seconds=10), "request-1")
        await reminders.tick(NOW + timedelta(seconds=10))
        assert not any(event["type"] == "reminder_due" for event in first_events)
        due = [event for event in second_events if event["type"] == "reminder_due"]
        assert len(due) == 1
        delivery = due[0]["reminder"]["delivery_id"]
        assert not await reminders.ack("device-b", "connection-2", item.reminder_id, delivery, "playing")
        assert not await reminders.ack("device-a", "connection-1", item.reminder_id, delivery, "playing")
        assert not await reminders.ack("device-a", "connection-2", item.reminder_id, "forged", "playing")
        assert await reminders.ack("device-a", "connection-2", item.reminder_id, delivery, "playing")
        assert await reminders.ack("device-a", "connection-2", item.reminder_id, delivery, "completed")
        assert not await reminders.ack("device-a", "connection-2", item.reminder_id, delivery, "playing")
        await reminders.tick(NOW + timedelta(minutes=1))
        assert len([event for event in second_events if event["type"] == "reminder_due"]) == 1
        saved = await reminders.get("device-a", item.reminder_id)
        assert saved.delivery_status == "completed"
        assert saved.status == "triggered"
    asyncio.run(scenario())


def test_offline_reminder_is_missed_and_reconnect_does_not_play_it():
    async def scenario():
        reminders = service()
        item = await reminders.create("device-a", "取快递", NOW + timedelta(seconds=10), "request-1")
        await reminders.tick(NOW + timedelta(seconds=11))
        assert (await reminders.get("device-a", item.reminder_id)).status == "missed"
        events = []

        async def send(event):
            events.append(event)

        reminders.connect("device-a", "connection-1", send)
        await reminders.tick(NOW + timedelta(minutes=1))
        assert events == []
    asyncio.run(scenario())


def test_cancel_update_due_and_timeout_have_explicit_results():
    async def scenario():
        reminders = service()
        events = []

        async def send(event):
            events.append(event)

        reminders.connect("device-a", "connection-1", send)
        item = await reminders.create("device-a", "取快递", NOW + timedelta(seconds=10), "request-1")
        await reminders.update("device-a", item.reminder_id, "拿钥匙", NOW + timedelta(seconds=20))
        await reminders.tick(NOW + timedelta(seconds=10))
        assert not any(event["type"] == "reminder_due" for event in events)
        await reminders.tick(NOW + timedelta(seconds=20))
        due = next(event for event in events if event["type"] == "reminder_due")
        assert due["reminder"]["content"] == "拿钥匙"
        with pytest.raises(ValueError):
            await reminders.update("device-a", item.reminder_id, "修改", NOW + timedelta(minutes=1))
        await reminders.tick(NOW + timedelta(minutes=3))
        assert (await reminders.get("device-a", item.reminder_id)).delivery_status == "unknown"
        assert not await reminders.ack("device-a", "connection-1", item.reminder_id,
                                       due["reminder"]["delivery_id"], "completed")
        await reminders.cancel("device-a", item.reminder_id)
        assert (await reminders.get("device-a", item.reminder_id)).status == "cancelled"
        assert any(event["type"] == "reminder_cancelled" for event in events)
    asyncio.run(scenario())


def test_disconnect_marks_pending_playback_unknown_without_cancelling_future_reminders():
    async def scenario():
        reminders = service()

        async def send(event):
            pass

        reminders.connect("device-a", "connection-1", send)
        soon = await reminders.create("device-a", "取快递", NOW + timedelta(seconds=10), "request-1")
        later = await reminders.create("device-a", "拿钥匙", NOW + timedelta(hours=1), "request-2")
        await reminders.tick(NOW + timedelta(seconds=10))
        await reminders.disconnect("device-a", "connection-1")
        assert (await reminders.get("device-a", soon.reminder_id)).delivery_status == "unknown"
        assert (await reminders.get("device-a", later.reminder_id)).status == "scheduled"
    asyncio.run(scenario())


def test_cancellation_before_storage_finishes_does_not_leave_an_orphan_creation():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Store(InMemoryReminderStore):
            async def create(self, item):
                entered.set()
                await release.wait()
                return await super().create(item)

        reminders = ReminderService(Store(), clock=lambda: NOW)
        task = asyncio.create_task(reminders.create("device-a", "取快递", NOW + timedelta(minutes=1), "r1"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await reminders.list("device-a") == ()
    asyncio.run(scenario())


def test_nested_lifespans_share_one_worker_until_the_last_close():
    async def scenario():
        reminders = service()
        await reminders.start()
        first = reminders._worker
        try:
            await reminders.start()
            assert reminders._worker is first
            await reminders.close()
            assert not first.done()
            await reminders.close()
            assert first.done()
        finally:
            await reminders.close()
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
    asyncio.run(scenario())


def test_waiting_receipt_renews_delivery_and_timeout_notifies_the_target_page():
    async def scenario():
        current = [NOW]
        reminders = ReminderService(InMemoryReminderStore(), clock=lambda: current[0])
        events = []

        async def send(event):
            events.append(event)

        reminders.connect('device', 'page', send)
        item = await reminders.create('device', '取快递', NOW + timedelta(seconds=1), 'r')
        current[0] += timedelta(seconds=1)
        await reminders.tick()
        due = await reminders.get('device', item.reminder_id)
        current[0] += timedelta(seconds=80)
        assert not await reminders.ack('device', 'other-page', item.reminder_id, due.delivery_id, 'waiting')
        assert await reminders.ack('device', 'page', item.reminder_id, due.delivery_id, 'waiting')
        await reminders.tick(NOW + timedelta(seconds=100))
        assert (await reminders.get('device', item.reminder_id)).delivery_status == 'pending'
        assert await reminders.ack('device', 'page', item.reminder_id, due.delivery_id, 'playing')
        assert not await reminders.ack('device', 'page', item.reminder_id, due.delivery_id, 'waiting')
        await reminders.tick(NOW + timedelta(seconds=180))
        assert (await reminders.get('device', item.reminder_id)).delivery_status == 'unknown'
        assert any(event['type'] == 'reminder_delivery_ended' and event['delivery_id'] == due.delivery_id
                   and event['status'] == 'unknown' for event in events)
    asyncio.run(scenario())
