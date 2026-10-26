import asyncio
import json

import pytest

from newtalk.chat import ChatMessage, ChatService
from newtalk.memory import MemorySearchResult
from newtalk.memory import MemoryItem
from newtalk.context_debug import ContextDebugger
from newtalk.chat import OpenAICompatibleChatModel
from types import SimpleNamespace
from tests.test_memory import RecordingMemoryProvider
from tests.test_openai_compatible import StubClient, StubStream


class DirectModel:
    def __init__(self):
        self.requests = []

    async def stream(self, messages, *, tools=()):
        self.requests.append((tuple(messages), tuple(tools)))
        yield '根据当前问题回答'

    async def aclose(self):
        pass


def member_turn(service, *, identity='member-1', text='我上次面试哪里没准备好？'):
    return service.create_turn(session_id='session-1', device_id='device-1',
                               speaker_identity_id=identity, user_text=text)


def test_preload_searches_before_a_single_model_call_with_server_owned_scope():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Provider(RecordingMemoryProvider):
            async def search(self, scope, **kwargs):
                entered.set()
                await release.wait()
                return await super().search(scope, **kwargs)

        provider, model = Provider(), DirectModel()
        service = ChatService(model, memory_provider=provider, memory_retrieval_mode='preload')
        turn = member_turn(service)
        original = turn.messages
        async def collect():
            return [text async for text in service.stream_reply(turn)]
        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert model.requests == []
            release.set()
            assert await asyncio.wait_for(task, 1) == ['根据当前问题回答']
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(provider.searches) == len(model.requests) == 1
        scope, query, session_id, _, _ = provider.searches[0]
        assert (scope.device_id, scope.identity_id, session_id) == ('device-1', 'member-1', 'session-1')
        assert query == turn.user_text
        messages, tools = model.requests[0]
        assert tools == ()
        assert not any(message.tool_calls or message.tool_call_id for message in messages)
        assert messages[-2].context_kind == 'memory'
        assert messages[-2].role == 'user'
        assert json.loads(messages[-2].content.split('\n', 1)[1])['status'] == 'ok'
        assert messages[-1] == original[-1]
        assert messages[:-2] == original[:-1]
        assert turn.messages == original
    asyncio.run(scenario())


@pytest.mark.parametrize('identity,enabled', [(None, True), ('member-1', False)])
def test_guest_or_disabled_memory_skips_preload(identity, enabled):
    async def scenario():
        provider, model = RecordingMemoryProvider(), DirectModel()
        provider.enabled = enabled
        service = ChatService(model, memory_provider=provider, memory_retrieval_mode='preload')
        turn = member_turn(service, identity=identity)
        assert [text async for text in service.stream_reply(turn)]
        assert provider.searches == []
        assert model.requests == [(turn.messages, ())]
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['no_match', 'unavailable', 'timeout'])
def test_preload_failure_or_empty_result_still_uses_one_model_request(outcome):
    async def scenario():
        cancelled = asyncio.Event()

        class Provider(RecordingMemoryProvider):
            async def search(self, scope, **kwargs):
                if outcome == 'no_match':
                    return MemorySearchResult(())
                if outcome == 'unavailable':
                    raise RuntimeError('provider failed')
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        model = DirectModel()
        service = ChatService(model, memory_provider=Provider(), memory_retrieval_mode='preload',
                              memory_preload_timeout_seconds=0.02)
        assert [text async for text in service.stream_reply(member_turn(service))]
        assert len(model.requests) == 1
        result = json.loads(model.requests[0][0][-2].content.split('\n', 1)[1])
        if outcome == 'no_match':
            assert result['status'] == 'no_match'
        else:
            assert result['code'] == outcome
        if outcome == 'timeout':
            assert cancelled.is_set()
    asyncio.run(scenario())


def test_turn_cancellation_cancels_preload_without_starting_model():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Provider(RecordingMemoryProvider):
            async def search(self, scope, **kwargs):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        model = DirectModel()
        service = ChatService(model, memory_provider=Provider(), memory_retrieval_mode='preload')
        async def collect():
            return [text async for text in service.stream_reply(member_turn(service))]
        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert model.requests == []
    asyncio.run(scenario())


def test_preload_records_one_actual_provider_request_and_separate_timings(tmp_path):
    async def scenario():
        client = StubClient(StubStream([SimpleNamespace(type='content.delta', delta='自然回答')]))
        original_stream = client.chat.completions.stream
        def stream_without_fabricated_calls(**request):
            assert not any(message.get('tool_calls') for message in request['messages']), 'reasoning_content missing for fabricated assistant call'
            return original_stream(**request)
        client.chat.completions.stream = stream_without_fabricated_calls
        provider = RecordingMemoryProvider()
        path = tmp_path / 'context.jsonl'
        service = ChatService(
            OpenAICompatibleChatModel(api_key='not-logged', model='test', client=client),
            memory_provider=provider, memory_retrieval_mode='preload',
            context_debug=ContextDebugger(enabled=True, path=path),
        )
        turn = member_turn(service, text='英语练习' * 200)
        try:
            assert [text async for text in service.stream_reply(turn)] == ['自然回答']
        finally:
            await service.aclose()
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        requests = [event for event in events if event['event'] == 'llm_request']
        assert len(requests) == 1
        assert requests[0]['request_index'] == 1
        assert requests[0]['request'] == client.chat.completions.request
        assert 'tools' not in requests[0]['request']
        assert all('tool_call_id' not in message and 'tool_calls' not in message
                   for message in requests[0]['request']['messages'])
        assert requests[0]['budget']['components']['preloaded_memory'] > 0
        assert requests[0]['budget']['after_tokens_estimate'] <= requests[0]['budget']['input_token_limit']
        retrieved = next(event for event in events if event['event'] == 'memory_search_result')
        assert retrieved['origin'] == 'server'
        assert retrieved['mode'] == 'preload'
        assert retrieved['query_truncated'] is True
        assert len(provider.searches[0][1]) == 500
        assert retrieved['elapsed_ms'] >= 0
        assert events.index(retrieved) < events.index(requests[0])
        assert next(event for event in events if event['event'] == 'llm_round_first_token')['round_index'] == 1
        assert not any(event['event'] == 'llm_tool_decision_completed' for event in events)
    asyncio.run(scenario())


def test_concurrent_members_do_not_share_preloaded_results():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Provider(RecordingMemoryProvider):
            async def search(self, scope, **kwargs):
                if scope.identity_id == 'member-1':
                    entered.set()
                    await release.wait()
                return MemorySearchResult((MemoryItem(scope.identity_id, 'fact', 'fact', scope.identity_id, 1),))

        model = DirectModel()
        service = ChatService(model, memory_provider=Provider(), memory_retrieval_mode='preload')
        async def collect(identity):
            return [text async for text in service.stream_reply(member_turn(service, identity=identity))]
        first = asyncio.create_task(collect('member-1'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(collect('member-2'), 1)
            release.set()
            await asyncio.wait_for(first, 1)
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        assert [json.loads(messages[-2].content.split('\n', 1)[1])['memories'][0]['content']
                for messages, _ in model.requests] == ['member-2', 'member-1']
    asyncio.run(scenario())
