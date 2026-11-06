import asyncio
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from newtalk.chat.models import ChatMessage, ModelToolCall
from newtalk.memory.curation import LlmMemoryCurator


# 独立遵守 store 的冻结数据契约，不依赖并行开发的持久化实现。
@dataclass(frozen=True)
class Source:
    turn_id: str = 't1'
    device_id: str = 'd'
    identity_id: str = 'member'
    session_id: str = 'session'
    segment_id: str = 'segment'
    speaker_name: str = '小明'
    user_text: str = '明天我要去杭州出差。'
    chat_time: datetime = datetime(2026, 10, 27, 23, tzinfo=timezone(timedelta(hours=8)))
    assistant_text: str | None = '你可能喜欢旅游。'
    settled: bool = True
    context_only: bool = False


@dataclass(frozen=True)
class Batch:
    batch_id: str = 'batch'
    device_id: str = 'd'
    identity_id: str = 'member'
    session_id: str = 'session'
    segment_id: str = 'segment'
    speaker_name: str = '小明'
    sources: tuple = (Source(),)
    attempts: int = 0
    lease_token: str = 'private-lease'
    prepared_payload: dict | None = None


class Model:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []
        self.stream_closed = False
        self.close_calls = 0

    # 故意不接受 tools 参数，验证调用方根本没有提供工具。
    async def stream(self, messages):
        self.calls.append(messages)
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.stream_closed = True

    async def aclose(self):
        self.close_calls += 1


def candidate(**changes):
    return {
        'kind': 'event', 'content': '小明计划明天去杭州出差。',
        'source_turn_ids': ['t1'],
        'evidence': [{'turn_id': 't1', 'quote': '明天我要去杭州出差'}],
        'event_time': '2026-10-28', 'state': 'planned', **changes,
    }


def curate_document(document, batch=None):
    model = Model([json.dumps(document, ensure_ascii=False)])
    return asyncio.run(LlmMemoryCurator(model).curate(batch or Batch()))


def test_multiturn_normalization_and_no_tools():
    async def scenario():
        sources = (
            replace(Source(), turn_id='old', user_text='那家是杭州分公司。',
                    context_only=True, chat_time=datetime(2030, 1, 1, tzinfo=UTC)),
            Source(),
            replace(Source(), turn_id='t2', user_text='我会在那里待三天。',
                    chat_time=datetime(2026, 10, 28, 0, tzinfo=timezone(timedelta(hours=8)))),
        )
        item = candidate(source_turn_ids=['old', 't1', 't2'], evidence=[
            {'turn_id': 'old', 'quote': '杭州分公司'},
            {'turn_id': 't1', 'quote': '明天我要去杭州出差'},
            {'turn_id': 't2', 'quote': '在那里待三天'},
        ], content='小明计划到杭州分公司出差三天。')
        raw = json.dumps({'memories': [item]}, ensure_ascii=False)
        model = Model(['```json\n', raw[:20], raw[20:], '\n```'])
        result = await LlmMemoryCurator(model).curate(Batch(sources=sources))
        assert result == {'memories': [{**item, 'observed_at': '2026-10-27T16:00:00+00:00'}]}
        assert model.stream_closed
        messages, = model.calls
        assert isinstance(messages, tuple) and len(messages) == 2
        assert all(isinstance(message, ChatMessage) for message in messages)
        assert [message.role for message in messages] == ['system', 'user']
        assert all(not message.tool_calls for message in messages)
        material = json.loads(messages[1].content)
        assert material['identity_id'] == 'member'
        assert material['sources'][1]['user_text'] == sources[1].user_text
        assert material['sources'][1]['chat_time'] == sources[1].chat_time.isoformat()
        assert material['sources'][1]['assistant_text'] == sources[1].assistant_text
        assert material['sources'][0]['context_only'] is True
        assert 'lease_token' not in material and 'prepared_payload' not in material
        for rule in ('问候', '纯控制', '回忆问答', '一次行为', '不同日期',
                     'planned', 'completed', 'unknown', 'context_only', 'chat_time',
                     '助手', '自信', '不执行工具'):
            assert rule in messages[0].content
    asyncio.run(scenario())


def test_context_only_cannot_be_the_only_cited_source():
    batch = Batch(sources=(replace(Source(), context_only=True),
                           replace(Source(), turn_id='new')))
    with pytest.raises(ValueError):
        curate_document({'memories': [candidate()]}, batch)


@pytest.mark.parametrize('quote', ['你可能喜欢旅游', '明天我要去上海出差', '', '   ', 5, None])
def test_evidence_must_be_a_nonempty_exact_user_substring_not_assistant_guess(quote):
    with pytest.raises(ValueError):
        curate_document({'memories': [candidate(evidence=[{'turn_id': 't1', 'quote': quote}])]})


@pytest.mark.parametrize('changes', [
    {'source_turn_ids': ['stranger']}, {'source_turn_ids': []},
    {'source_turn_ids': ['t1', 't1']}, {'source_turn_ids': 't1'},
    {'source_turn_ids': [1]}, {'source_turn_ids': [True]},
    {'evidence': []}, {'evidence': 'not a list'},
    {'evidence': [{'turn_id': 'stranger', 'quote': '明天'}]},
    {'evidence': [{'turn_id': 't1', 'quote': '明天', 'confidence': 1}]},
    {'evidence': [None]}, {'evidence': [{'turn_id': True, 'quote': '明天'}]},
    {'kind': 'profile'}, {'kind': []}, {'kind': 1},
    {'content': ''}, {'content': '   '}, {'content': 3}, {'content': None},
    {'content': 'a' * 1501}, {'content': 'bad\ntext'}, {'content': 'bad\x00text'},
    {'content': 'bad\u202etext'}, {'content': '\ud800'},
    {'event_time': ''}, {'event_time': 'a' * 101}, {'event_time': 20261028},
    {'event_time': 'bad\tdate'}, {'state': 'done'}, {'state': False},
    {'observed_at': '2099-01-01'}, {'confidence': 0.99}, {'identity_id': 'stranger'},
])
def test_strict_candidate_format(changes):
    with pytest.raises(ValueError):
        curate_document({'memories': [candidate(**changes)]})


@pytest.mark.parametrize('document', [None, [], {}, {'memories': None},
    {'memories': {}}, {'memories': [], 'extra': 1}, {'memories': [None]},
    {'memories': [candidate()] * 17},
    {'memories': [{key: value for key, value in candidate().items() if key != 'state'}]},
])
def test_strict_document_format(document):
    with pytest.raises(ValueError):
        curate_document(document)


@pytest.mark.parametrize('raw', [
    '{"memories": [],}', '{"memories": [], "memories": []}',
    '{"memories": [], "extra": NaN}', '{"memories": []} trailing',
    'explanation {"memories": []}', '```json\n```json\n{"memories": []}\n```\n```',
    '```\n{"memories": []}\n```', '{"memories": []',
])
def test_json_is_not_silently_repaired(raw):
    with pytest.raises(ValueError):
        asyncio.run(LlmMemoryCurator(Model([raw])).curate(Batch()))


def test_all_sources_need_evidence_and_evidence_cannot_cite_unlisted_source():
    batch = Batch(sources=(Source(), replace(Source(), turn_id='t2')))
    for item in (
        candidate(source_turn_ids=['t1', 't2']),
        candidate(evidence=[{'turn_id': 't1', 'quote': '明天'},
                            {'turn_id': 't2', 'quote': '明天'}]),
    ):
        with pytest.raises(ValueError):
            curate_document({'memories': [item]}, batch)


@pytest.mark.parametrize('field', ['device_id', 'identity_id', 'session_id', 'segment_id', 'speaker_name'])
def test_foreign_member_or_scope_rejected_before_model_call(field):
    model = Model(['{"memories": []}'])
    batch = Batch(sources=(replace(Source(), **{field: 'stranger'}),))
    with pytest.raises(ValueError):
        asyncio.run(LlmMemoryCurator(model).curate(batch))
    assert model.calls == []


def test_ambiguous_source_ids_or_naive_source_times_are_rejected():
    for sources in ((Source(), Source()), (replace(Source(), turn_id=''),),
                    (replace(Source(), chat_time=datetime(2026, 10, 27)),)):
        with pytest.raises(ValueError):
            curate_document({'memories': []}, Batch(sources=sources))


@pytest.mark.parametrize('state', ['planned', 'completed', 'unknown'])
@pytest.mark.parametrize('kind', ['fact', 'preference', 'event'])
def test_supported_kinds_states_and_nullable_event_time(kind, state):
    item = candidate(kind=kind, state=state, event_time=None, content='a' * 1500)
    assert curate_document({'memories': [item]}) == {
        'memories': [{**item, 'observed_at': '2026-10-27T15:00:00+00:00'}],
    }


def test_empty_candidates_and_sixteen_candidate_limit():
    assert curate_document({'memories': []}) == {'memories': []}
    item = candidate(event_time='a' * 100)
    assert len(curate_document({'memories': [item] * 16})['memories']) == 16


def test_observation_time_uses_only_cited_new_sources_and_orders_actual_instants():
    batch = Batch(sources=(
        Source(),
        replace(Source(), turn_id='earlier', chat_time=datetime(2026, 10, 27, 14, tzinfo=UTC)),
        replace(Source(), turn_id='unreferenced', chat_time=datetime(2030, 1, 1, tzinfo=UTC)),
    ))
    item = candidate(source_turn_ids=['earlier', 't1'], evidence=[
        {'turn_id': 'earlier', 'quote': '明天'}, {'turn_id': 't1', 'quote': '明天'},
    ])
    result = curate_document({'memories': [item]}, batch)
    assert result['memories'][0]['observed_at'] == '2026-10-27T15:00:00+00:00'
    assert result['memories'][0]['event_time'] == '2026-10-28'


def test_output_budget_counts_all_chunks_including_fences_and_whitespace():
    async def scenario():
        raw = '{"memories": []}'
        exact = Model([raw, ' ' * (65536 - len(raw))])
        assert await LlmMemoryCurator(exact).curate(Batch()) == {'memories': []}
        for chunks in ([raw, ' ' * (65537 - len(raw))], ['x' * 65537]):
            model = Model(chunks)
            with pytest.raises(ValueError):
                await LlmMemoryCurator(model).curate(Batch())
            assert model.stream_closed
    asyncio.run(scenario())


@pytest.mark.parametrize('chunk', [ModelToolCall('call', 'delete', '{"secret":"private"}'), None, 123])
def test_tool_calls_and_nontext_chunks_fail_and_close_stream(chunk):
    model = Model([chunk, '{"memories": []}'])
    with pytest.raises(ValueError):
        asyncio.run(LlmMemoryCurator(model).curate(Batch()))
    assert model.stream_closed


@pytest.mark.parametrize('chunk', ['x' * 65537, ModelToolCall('call', 'delete', '{}')],
                         ids=['oversized-output', 'tool-call'])
def test_rejection_stops_consumption_immediately(chunk):
    class GuardedModel(Model):
        async def stream(self, messages):
            try:
                yield chunk
                pytest.fail('must stop before requesting the next chunk')
            finally:
                self.stream_closed = True

    model = GuardedModel([])
    with pytest.raises(ValueError):
        asyncio.run(LlmMemoryCurator(model).curate(Batch()))
    assert model.stream_closed


class BlockingModel(Model):
    def __init__(self):
        super().__init__([])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def stream(self, messages):
        try:
            yield '{"memories":'
            self.entered.set()
            await self.release.wait()
            yield '[]}'
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.stream_closed = True


def test_cancellation_reaches_stream_and_close_delegates_to_model():
    async def scenario():
        model = BlockingModel()
        curator = LlmMemoryCurator(model)
        task = asyncio.create_task(curator.curate(Batch()))
        await asyncio.wait_for(model.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model.cancelled.is_set() and model.stream_closed
        await curator.close()
        await curator.close()
        assert model.close_calls == 1
        with pytest.raises(RuntimeError):
            await curator.curate(Batch())
    asyncio.run(scenario())


def test_whole_request_timeout_is_event_controlled(monkeypatch):
    async def scenario():
        model = BlockingModel()
        real_timeout = asyncio.timeout
        budgets = []
        contexts = []

        def controlled_timeout(seconds):
            budgets.append(seconds)
            context = real_timeout(None)
            contexts.append(context)
            return context

        monkeypatch.setattr(asyncio, 'timeout', controlled_timeout)
        curator = LlmMemoryCurator(model, timeout_seconds=45)
        task = asyncio.create_task(curator.curate(Batch()))
        await asyncio.wait_for(model.entered.wait(), 1)
        assert budgets == [45]
        contexts[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError):
            await task
        assert model.cancelled.is_set() and model.stream_closed
    asyncio.run(scenario())


@pytest.mark.parametrize('seconds', [0, -1, float('inf'), float('nan'), True, '45'])
def test_invalid_timeout_is_rejected(seconds):
    with pytest.raises(ValueError):
        LlmMemoryCurator(Model([]), timeout_seconds=seconds)


def test_provider_failure_and_validation_do_not_expose_private_text(caplog, capsys):
    class FailingModel(Model):
        async def stream(self, messages):
            try:
                raise RuntimeError('private-user private-assistant private-error')
                yield
            finally:
                self.stream_closed = True

    model = FailingModel([])
    with pytest.raises(RuntimeError) as error:
        asyncio.run(LlmMemoryCurator(model).curate(Batch()))
    assert model.stream_closed
    assert 'private' not in str(error.value)
    invalid = Model(['private-user private-assistant'])
    with pytest.raises(ValueError) as error:
        asyncio.run(LlmMemoryCurator(invalid).curate(Batch()))
    assert 'private' not in str(error.value)
    assert 'private' not in caplog.text
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_provider_timeout_and_close_failure_are_sanitized():
    class FailingModel(Model):
        async def stream(self, messages):
            raise TimeoutError('private-provider-timeout')
            yield

        async def aclose(self):
            raise RuntimeError('private-provider-close')

    curator = LlmMemoryCurator(FailingModel([]))
    with pytest.raises(TimeoutError) as error:
        asyncio.run(curator.curate(Batch()))
    assert 'private' not in str(error.value)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(curator.close())
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('candidate_count', [0, 2])
def test_safe_started_and_completed_logs(candidate_count, caplog, monkeypatch):
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr('newtalk.memory.curation.perf_counter', lambda: next(ticks), raising=False)
    sources = (Source(), replace(Source(), turn_id='old', context_only=True))
    items = [candidate()] * candidate_count
    with caplog.at_level(logging.INFO, logger='newtalk.memory.curation'):
        result = curate_document({'memories': items}, Batch(sources=sources))
    assert len(result['memories']) == candidate_count
    records = [record for record in caplog.records if record.name == 'newtalk.memory.curation']
    assert [record.getMessage() for record in records] == [
        'memory_curation_started batch_id="batch" source_count=2',
        ('memory_curation_completed batch_id="batch" source_count=2 '
         f'candidate_count={candidate_count} elapsed_ms=125.0'),
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    for secret in (sources[0].user_text, sources[0].assistant_text,
                   candidate()['content'], candidate()['evidence'][0]['quote'], 'private-lease'):
        assert secret not in caplog.text


@pytest.mark.parametrize('failure', ['provider', 'json', 'validation'])
def test_unsuccessful_curation_never_logs_completed_or_private_body(failure, caplog):
    class FailingModel(Model):
        async def stream(self, messages):
            raise RuntimeError('private-provider-exception-body')
            yield

    if failure == 'provider':
        model = FailingModel([])
    elif failure == 'json':
        model = Model(['private-malformed-json-response'])
    else:
        model = Model([json.dumps({'memories': [candidate(content='private-content', evidence=[])]})])
    with caplog.at_level(logging.INFO, logger='newtalk.memory.curation'), pytest.raises((ValueError, RuntimeError)):
        asyncio.run(LlmMemoryCurator(model).curate(Batch()))
    records = [record for record in caplog.records if record.name == 'newtalk.memory.curation']
    assert [record.getMessage() for record in records] == [
        'memory_curation_started batch_id="batch" source_count=1',
    ]
    assert records[0].exc_info is None and records[0].stack_info is None
    assert 'private' not in caplog.text


@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
def test_interrupted_curation_never_logs_completed(outcome, caplog, monkeypatch):
    async def scenario():
        model = BlockingModel()
        contexts = []
        real_timeout = asyncio.timeout

        def controlled_timeout(seconds):
            context = real_timeout(None)
            contexts.append(context)
            return context

        monkeypatch.setattr(asyncio, 'timeout', controlled_timeout)
        task = asyncio.create_task(LlmMemoryCurator(model).curate(Batch()))
        await asyncio.wait_for(model.entered.wait(), 1)
        if outcome == 'cancel':
            task.cancel()
        else:
            contexts[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else TimeoutError):
            await task
        assert model.cancelled.is_set() and model.stream_closed

    with caplog.at_level(logging.INFO, logger='newtalk.memory.curation'):
        asyncio.run(scenario())
    assert [record.getMessage() for record in caplog.records
            if record.name == 'newtalk.memory.curation'] == [
        'memory_curation_started batch_id="batch" source_count=1',
    ]


def test_log_batch_id_is_escaped_to_prevent_line_injection(caplog):
    batch_id = 'batch"\nforged_event'
    with caplog.at_level(logging.INFO, logger='newtalk.memory.curation'):
        assert curate_document({'memories': []}, Batch(batch_id=batch_id)) == {'memories': []}
    records = [record for record in caplog.records if record.name == 'newtalk.memory.curation']
    assert len(records) == 2
    for record in records:
        assert '\n' not in record.getMessage()
        assert f'batch_id={json.dumps(batch_id)} ' in record.getMessage()
