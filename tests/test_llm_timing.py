import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest

from newtalk.chat import ChatMessage, ChatService, OpenAICompatibleChatModel
from newtalk.context_debug import ContextDebugger
from tests.test_openai_compatible import StubClient, StubStream


def test_llm_diagnostics_separate_first_event_reasoning_and_answer(tmp_path, caplog):
    async def scenario():
        stream = StubStream([
            SimpleNamespace(type='chunk', chunk=SimpleNamespace(choices=[
                SimpleNamespace(delta=SimpleNamespace(reasoning_content='private-reasoning')),
            ])),
            SimpleNamespace(type='content.delta', delta='private-answer'),
        ])
        client = StubClient(stream)
        debugger = ContextDebugger(enabled=True, path=tmp_path / 'trace.jsonl')
        service = ChatService(OpenAICompatibleChatModel(api_key='private-key', model='test', client=client),
                              context_debug=debugger)
        turn = service.create_turn(session_id='session', device_id='device', user_text='private-input')
        try:
            assert [part async for part in service.stream_reply(turn)] == ['private-answer']
            events = debugger.events_for_device('device', turn.turn_id)
            summary = next(event for event in events if event['event'] == 'llm_request_timing')
            request = next(event for event in events if event['event'] == 'llm_request')
            assert summary['call_id'] == request['call_id']
            assert summary['outcome'] == 'completed'
            assert summary['first_event_ms'] <= summary['first_reasoning_ms'] <= summary['first_text_ms']
            assert summary['attempts'] == []  # 注入的 Stub 不伪造 HTTP 观测。
            assert len(summary['payload_sha256']) == 64
            assert summary['request_bytes'] > 0
            assert 'private-' not in json.dumps(summary)
        finally:
            await service.aclose()
    asyncio.run(scenario())
    assert 'private-key' not in caplog.text
    assert 'private-reasoning' not in caplog.text
    assert 'private-answer' not in caplog.text


def sse(text='reply'):
    chunk = {'id': 'completion-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test',
             'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}, 'finish_reason': None}]}
    done = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
    return f'data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n'.encode()


def use_transport(monkeypatch, handler):
    from openai import DefaultAsyncHttpxClient
    monkeypatch.setattr('newtalk.chat.openai_compatible.DefaultAsyncHttpxClient',
                        lambda **kwargs: DefaultAsyncHttpxClient(transport=httpx.MockTransport(handler), **kwargs))


def test_real_sdk_retries_have_distinct_attempts_and_metadata_only(tmp_path, monkeypatch, caplog):
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        trace = request.extensions['trace']
        await trace('connection.connect_tcp.started', {'host': 'must-not-log-this-host'})
        await trace('connection.connect_tcp.complete', {'return_value': 'private-transport'})
        await trace('connection.start_tls.started', {})
        await trace('connection.start_tls.complete', {})
        await trace('http11.send_request_body.complete', {})
        if len(calls) == 1:
            return httpx.Response(429, headers={'x-request-id': 'retry-1', 'set-cookie': 'private-cookie'},
                                  json={'error': {'message': 'private-error', 'type': 'rate_limit'}})
        return httpx.Response(200, headers={'x-request-id': 'success-2', 'content-type': 'text/event-stream'},
                              content=sse())

    use_transport(monkeypatch, handler)

    async def no_retry_delay(*args, **kwargs):
        pass  # 只替换测试中的 SDK 退避等待，不更改生产重试配置。

    monkeypatch.setattr('openai.AsyncOpenAI._sleep_for_retry', no_retry_delay)

    async def scenario():
        debugger = ContextDebugger(enabled=True, path=tmp_path / 'trace.jsonl')
        model = OpenAICompatibleChatModel(api_key='private-key', base_url='https://example.test/v1', model='test')
        service = ChatService(model, context_debug=debugger)
        turn = service.create_turn(session_id='session', device_id='device', user_text='private-input')
        try:
            assert calls == []
            assert [text async for text in service.stream_reply(turn)] == ['reply']
            summary = next(event for event in debugger.events_for_device('device')
                           if event['event'] == 'llm_request_timing')
            assert [attempt['status_code'] for attempt in summary['attempts']] == [429, 200]
            assert [attempt['sdk_retry_count'] for attempt in summary['attempts']] == [0, 1]
            assert [attempt['provider_request_id'] for attempt in summary['attempts']] == ['retry-1', 'success-2']
            assert all(attempt['trace_seen'] for attempt in summary['attempts'])
            assert summary['stream_open_ms'] >= summary['attempts'][1]['response_headers_ms']
            assert summary['first_text_ms'] >= summary['first_event_ms']
            assert calls[0] == calls[1]
            assert calls[0] == {'model': 'test', 'messages': [{'role': 'user', 'content': turn.messages[-1].content}], 'stream': True}
            assert 'private-' not in json.dumps(summary)
            assert 'must-not-log-this-host' not in json.dumps(summary)
        finally:
            await service.aclose()

    asyncio.run(scenario())
    assert 'private-key' not in caplog.text
    assert 'private-cookie' not in caplog.text


def test_concurrent_http_timing_and_cancellation_are_isolated(tmp_path, monkeypatch):
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        content = json.loads(request.content)['messages'][-1]['content']
        if content.endswith('first'):
            entered.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
        return httpx.Response(200, headers={'x-request-id': 'second-request', 'content-type': 'text/event-stream'},
                              content=sse())

    use_transport(monkeypatch, handler)

    async def scenario():
        from newtalk.chat.llm_timing import active_request_timing

        debugger = ContextDebugger(enabled=True, path=tmp_path / 'trace.jsonl')
        service = ChatService(OpenAICompatibleChatModel(api_key='unused', base_url='https://example.test/v1', model='test'),
                              context_debug=debugger)
        first = service.create_turn(session_id='s1', device_id='d1', user_text='first')
        second = service.create_turn(session_id='s2', device_id='d2', user_text='second')

        async def collect(turn):
            return [part async for part in service.stream_reply(turn)]

        task = asyncio.create_task(collect(first))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert await collect(second) == ['reply']
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
            summaries = [next(e for e in debugger.events_for_device(device) if e['event'] == 'llm_request_timing')
                         for device in ['d1', 'd2']]
            assert [e['outcome'] for e in summaries] == ['cancelled', 'completed']
            assert summaries[0]['call_id'] != summaries[1]['call_id']
            assert summaries[0]['attempts'][0].get('status_code') is None
            assert summaries[1]['attempts'][0]['provider_request_id'] == 'second-request'
            assert active_request_timing.get() is None
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await service.aclose()

    asyncio.run(scenario())


def test_consumer_close_immediately_releases_inner_stream_and_context():
    async def scenario():
        from newtalk.chat.llm_timing import active_request_timing

        stream = StubStream([SimpleNamespace(type='content.delta', delta='first'),
                             SimpleNamespace(type='content.delta', delta='second')])
        model = OpenAICompatibleChatModel(api_key='unused', model='test', client=StubClient(stream))
        try:
            async with aclosing(model.stream((ChatMessage('user', 'hello'),))) as events:
                assert await anext(events) == 'first'
            assert stream.exited  # 不等 asyncio.run 的 shutdown_asyncgens 才清理。
            assert active_request_timing.get() is None
        finally:
            await model.aclose()

    asyncio.run(scenario())


def test_sdk_completion_resource_is_prepared_before_first_user_request():
    accesses = []
    stream = StubStream([SimpleNamespace(type='content.delta', delta='reply')])

    class Client(StubClient):
        def __init__(self):
            self.closed = False

        @property
        def chat(self):
            accesses.append('prepare-resource')
            return SimpleNamespace(completions=SimpleNamespace(stream=lambda **kwargs: stream))

    async def scenario():
        model = OpenAICompatibleChatModel(api_key='unused', model='test', client=Client())
        try:
            assert accesses == ['prepare-resource']
            assert [part async for part in model.stream((ChatMessage('user', 'hello'),))] == ['reply']
            assert accesses == ['prepare-resource']
        finally:
            await model.aclose()

    asyncio.run(scenario())


def test_timeout_keeps_sdk_retry_budget_and_cleans_up_timing(tmp_path, monkeypatch):
    from openai import APITimeoutError

    async def handler(request):
        raise httpx.ReadTimeout('provider timeout', request=request)

    async def no_retry_delay(*args, **kwargs):
        pass

    use_transport(monkeypatch, handler)
    monkeypatch.setattr('openai.AsyncOpenAI._sleep_for_retry', no_retry_delay)

    async def scenario():
        from newtalk.chat.llm_timing import active_request_timing

        debugger = ContextDebugger(enabled=True, path=tmp_path / 'trace.jsonl')
        model = OpenAICompatibleChatModel(api_key='unused', base_url='https://example.test/v1', model='test')
        turn = ChatService().create_turn(session_id='session', device_id='device', user_text='hello')
        token = debugger.activate(turn)
        try:
            with pytest.raises(APITimeoutError):
                _ = [part async for part in model.stream(turn.messages)]
            summary = next(e for e in debugger.events_for_device('device') if e['event'] == 'llm_request_timing')
            assert len(summary['attempts']) == 3
            assert [attempt['sdk_retry_count'] for attempt in summary['attempts']] == [0, 1, 2]
            assert summary['outcome'] == 'failed'
            assert 'first_text_ms' not in summary
            assert active_request_timing.get() is None
        finally:
            debugger.reset(token)
            debugger.close()
            await model.aclose()

    asyncio.run(scenario())


def test_probe_freezes_text_request_and_rejects_tools_or_extra_parameters(tmp_path):
    from scripts.probe_llm_latency import load_request

    path = tmp_path / 'source.jsonl'
    request = {'model': 'test', 'messages': [{'role': 'system', 'content': 'persona'},
                                          {'role': 'user', 'content': 'fixed question'}]}
    event = {'turn_id': 'turn', 'event': 'llm_request', 'request_index': 1, 'request': request}
    path.write_text(json.dumps(event) + '\n', encoding='utf-8')
    assert load_request(path, 'turn') == request
    with pytest.raises(ValueError, match='not found'):
        load_request(path, 'missing')
    event['request']['thinking'] = {'type': 'disabled'}
    path.write_text(json.dumps(event) + '\n', encoding='utf-8')
    assert load_request(path, 'turn')['thinking'] == {'type': 'disabled'}
    event['request']['tools'] = [{'type': 'function'}]
    path.write_text(json.dumps(event) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='text-only'):
        load_request(path, 'turn')


@pytest.mark.parametrize('mode', ['enabled', 'disabled'])
def test_thinking_extension_is_flattened_on_real_sdk_wire(mode, monkeypatch):
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse())

    use_transport(monkeypatch, handler)

    async def scenario():
        model = OpenAICompatibleChatModel(api_key='unused', base_url='https://example.test/v1',
                                          model='test', thinking=mode)
        try:
            assert [part async for part in model.stream((ChatMessage('user', 'hello'),))] == ['reply']
        finally:
            await model.aclose()

    asyncio.run(scenario())
    assert payloads[0]['thinking'] == {'type': mode}
    assert 'extra_body' not in payloads[0]


def test_app_factory_passes_thinking_configuration():
    from newtalk.app import create_chat_service
    from newtalk.config import AppConfig

    service = create_chat_service(AppConfig(llm_backend='openai', llm_api_key='unused',
                                           llm_model='test', llm_thinking='disabled'))
    try:
        assert service._model._thinking == 'disabled'
    finally:
        asyncio.run(service.aclose())
