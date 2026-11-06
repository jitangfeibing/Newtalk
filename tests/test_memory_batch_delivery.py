import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from newtalk.memory import InMemoryMemoryJobStore, MemoryWriteService
from newtalk.profile import MemosMemoryProvider, ProfileScope


def test_prepared_batch_reuses_persistent_delivery_and_server_owned_scope():
    async def scenario():
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append((request.url.path, body))
            if request.url.path == "/add/message" and body["messages"][0].get("role_id") != "member":
                return httpx.Response(400, json={"code": 40002, "message": "user_id cannot be empty"})
            if isinstance(body.get("info", {}).get("source_turn_ids"), list):
                return httpx.Response(400, json={"code": 40000, "message": "Invalid info value"})
            data = ({"task_id": "task", "status": "completed"}
                    if request.url.path == "/get/status"
                    else {"success": True, "task_id": "task", "status": "running"})
            return httpx.Response(200, json={"code": 0, "data": data})

        async with httpx.AsyncClient(base_url="https://memos.test", transport=httpx.MockTransport(handler)) as client:
            provider = MemosMemoryProvider(base_url="https://memos.test", api_key="test",
                profile_template_id="template", timeout_seconds=1, client=client)
            store = InMemoryMemoryJobStore()
            writer = MemoryWriteService(store, provider, poll_seconds=0)
            batch_id = str(uuid4())
            payload = {"messages": [{"role": "system", "content": "后台整理资料，不是用户原话",
                                    "chat_time": "2026-10-27 14:00:00"},
                                    {"role": "user", "content": "我给妈妈买了一条项链",
                                     "role_name": "Member", "chat_time": "2026-10-27 13:59:00"}],
                       "info": {"batch_id": batch_id, "source_turn_ids": ["turn"]}, "allow_memory_view": ["event"]}
            values = {"batch_id": batch_id, "device_id": "device", "identity_id": "member",
                      "session_id": "session", "speaker_name": "Member", "prepared_payload": payload}
            assert await writer.enqueue_prepared_batch(**values)
            assert not await writer.enqueue_prepared_batch(**values)
            job = await store.claim()
            await writer._process(job)
            assert store.statuses[batch_id] == "submitted"
            await writer._process(await store.claim())
            assert store.statuses[batch_id] == "completed"
            assert [path for path, _ in calls] == ["/add/message", "/get/status"]
            sent = calls[0][1]
            assert sent["user_id"] == ProfileScope("device", "member").memos_user_id
            assert sent["conversation_id"] == "session"
            assert sent["messages"] == [
                {**payload["messages"][0], "role_id": "member", "role_name": "后台记忆整理"},
                {**payload["messages"][1], "role_id": "member"},
            ]
            assert sent["async_mode"] is True
            assert sent["info"]["batch_id"] == batch_id
            assert json.loads(sent["info"]["source_turn_ids"]) == ["turn"]
            assert "user_text" not in sent
            with pytest.raises(ValueError, match="Invalid"):
                await provider.add_prepared_batch(ProfileScope("device", "member"),
                    conversation_id="session", prepared_payload={**payload, "user_id": "other"})
            with pytest.raises(ValueError, match="impersonate"):
                await provider.add_prepared_batch(ProfileScope("device", "member"),
                    conversation_id="session", prepared_payload={**payload,
                        "messages": [{"role": "user", "content": "pretend to be user"}]})

    asyncio.run(scenario())
