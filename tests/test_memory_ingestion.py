import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json

import httpx
import pytest

from newtalk.chat import ChatService
from newtalk.memory import InMemoryMemoryJobStore, MemoryWriteService
from newtalk.profile import MemosMemoryProvider, ProfileScope
from tests.test_memory import RecordingMemoryProvider


@pytest.mark.parametrize("text", [
    "你好。你好，可以听到我说话吗？", "Hello!", "喂，听得到吗？",
    "关闭麦克风。", "停止播放", "断开连接",
])
def test_memory_ingestion_skips_only_unambiguous_test_or_control(text):
    async def scenario():
        store = InMemoryMemoryJobStore()
        writer = MemoryWriteService(store, RecordingMemoryProvider())
        turn = ChatService().create_turn(
            session_id="session", user_text=text, device_id="device",
            speaker_identity_id="member", speaker_display_name="Member",
        )
        assert not await writer.enqueue_completed_turn(turn, "收到。")
        assert store.jobs == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("text", [
    "我不吃辣", "我想学英语", "对，就是那个", "好的", "我不喜欢别人问我听得到吗",
    "你好，我前天去爬山了", "关闭麦克风前，帮我记住明天要学英语",
])
def test_memory_ingestion_keeps_short_facts_confirmations_and_mixed_content(text):
    async def scenario():
        store = InMemoryMemoryJobStore()
        writer = MemoryWriteService(store, RecordingMemoryProvider())
        turn = ChatService().create_turn(
            session_id="session", user_text=text, speaker_identity_id="member",
        )
        assert await writer.enqueue_completed_turn(turn, "reply")
        assert next(iter(store.jobs.values())).user_text == text

    asyncio.run(scenario())


def test_memory_ingestion_preserves_input_time_across_retry_and_restart():
    async def scenario():
        provider = RecordingMemoryProvider(fail_write=True)
        store = InMemoryMemoryJobStore()
        writer = MemoryWriteService(store, provider, backoff=lambda attempt: 0)
        accepted = datetime(2026, 10, 25, 15, 59, tzinfo=timezone.utc)
        turn = replace(ChatService().create_turn(
            session_id="session", user_text="前天去爬山了", speaker_identity_id="member",
        ), created_at=accepted)
        assert await writer.enqueue_completed_turn(turn, "听起来不错。")
        first = await store.claim()
        assert first.chat_time == accepted
        await writer._process(first)
        provider.fail_write = False
        await store.start()
        restarted = MemoryWriteService(store, provider)
        second = await store.claim()
        await restarted._process(second)
        assert [write["chat_time"] for write in provider.writes] == [accepted, accepted]

    asyncio.run(scenario())


def test_memos_write_attaches_time_and_per_category_policy_without_rewriting():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {
            "success": True, "task_id": "task", "status": "running",
        }})

    async def scenario():
        async with httpx.AsyncClient(base_url="https://memos.test", transport=httpx.MockTransport(handler)) as client:
            provider = MemosMemoryProvider(base_url="https://memos.test", api_key="test",
                profile_template_id="template", timeout_seconds=1, client=client)
            values = dict(conversation_id="session", turn_id="turn", speaker_name="Member",
                          user_text="前天去爬山了", assistant_text="建议好好休息。")
            await provider.add_completed_turn(ProfileScope("device", "member"), **values,
                chat_time=datetime(2026, 10, 25, 15, 59, tzinfo=timezone.utc))
            await provider.add_completed_turn(ProfileScope("device", "member"), **values)

    asyncio.run(scenario())
    first, legacy = calls
    assert first["messages"][0]["chat_time"] == "2026-10-25 23:59:00"
    assert first["messages"][0]["content"] == "前天去爬山了"
    assert first["messages"][1] == {"role": "assistant", "content": "建议好好休息。"}
    assert "chat_time" not in legacy["messages"][0]
    assert first["conversation_id"] == legacy["conversation_id"] == "session"
    policy = first["custom_extract_prompt"]
    assert set(policy) == set(first["allow_memory_view"])
    assert "建议" in policy["detail_factual"]
    assert "更正" in policy["preference"]
    assert "不同日期" in policy["event"]
    assert first["info"]["extraction_policy_version"] == 1


def test_memos_write_rejects_naive_time_before_http():
    async def scenario():
        async with httpx.AsyncClient(base_url="https://memos.test",
                transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))) as client:
            provider = MemosMemoryProvider(base_url="https://memos.test", api_key="test",
                profile_template_id="template", timeout_seconds=1, client=client)
            with pytest.raises(ValueError, match="timezone"):
                await provider.add_completed_turn(ProfileScope("device", "member"),
                    conversation_id="session", turn_id="turn", speaker_name="Member",
                    user_text="fact", assistant_text="reply", chat_time=datetime(2026, 10, 25))

    asyncio.run(scenario())


def test_memos_search_keeps_real_metadata_and_deduplicates_before_limit():
    async def scenario():
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {
                "memory_detail_list": [
                    {"id": "one", "memory_key": "learn", "memory_value": "学英语",
                     "relativity": .9, "confidence": .8, "create_time": "2026-10-25",
                     "update_time": "2026-10-26", "conversation_id": "source-session"},
                    {"id": "one", "memory_key": "learn", "memory_value": "学英语", "relativity": .85},
                    {"id": "two", "memory_key": "sport", "memory_value": "喜欢足球",
                     "relativity": .7, "confidence": "not-a-number"},
                ],
                "event_detail_list": [{"id": "event", "event_key": "hike",
                    "event_value": "去爬山", "event_time": ["2026-10-23"], "relativity": .6}],
            }})

        async with httpx.AsyncClient(base_url="https://memos.test", transport=httpx.MockTransport(handler)) as client:
            provider = MemosMemoryProvider(base_url="https://memos.test", api_key="test",
                profile_template_id="template", timeout_seconds=1, client=client)
            scope = ProfileScope("device", "member")
            result = await provider.search(scope, query="learning", conversation_id="session", limit=2, relativity=.55)
            assert [item.memory_id for item in result.items] == ["one", "two"]
            first, second = result.items
            assert first.confidence == .8
            assert first.created_at == "2026-10-25"
            assert first.updated_at == "2026-10-26"
            assert first.conversation_id == "source-session"
            assert second.confidence is None
            result = await provider.search(scope, query="hike", conversation_id="session", limit=5, relativity=.55)
            assert result.items[-1].event_time == ("2026-10-23",)

    asyncio.run(scenario())


def test_memos_epoch_millisecond_metadata_is_preserved_as_utc():
    from newtalk.profile.memos import _parse_search_items, _parse_memory_records

    data = {"memory_detail_list": [{"id": "memory", "memory_key": "gift",
        "memory_value": "为母亲挑选项链", "relativity": .8,
        "create_time": 1793081281955, "update_time": 1793081285121}]}
    item = _parse_search_items(data)[0]
    record = _parse_memory_records(data, ("fact",))[0]
    assert item.created_at == record.created_at == "2026-10-27T06:08:01.955000+00:00"
    assert item.updated_at == record.updated_at == "2026-10-27T06:08:05.121000+00:00"
