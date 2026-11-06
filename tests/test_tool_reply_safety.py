import asyncio
import json
import logging

import pytest

from newtalk.chat import ChatService, ModelToolCall, TextDelta, TurnCompleted
from newtalk.tools import ToolRegistry
from newtalk.tools.podcasts import PodcastControlTool, PodcastSearchTool
from newtalk.tts import FakeTTS
from tests.test_podcasts import Harness


LEAK = '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="podcast_control">secret-arguments</｜｜DSML｜｜ invoke>'


class TextModel:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def stream(self, messages, *, tools=()):
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.closed = True

    async def aclose(self):
        pass


@pytest.mark.parametrize('marker', ['<｜｜DSML｜｜ calls>', '<｜DSML｜tool_calls>', '<|DSML|tool_calls>', '<||DSML|| calls>'])
def test_protocol_marker_is_blocked_at_every_stream_split_before_text_and_tts(marker, caplog):
    async def scenario():
        class Speech(FakeTTS):
            def __init__(self):
                super().__init__()
                self.texts = []

            async def stream(self, text_chunks, *, turn_id):
                async def record():
                    async for text in text_chunks:
                        self.texts.append(text)
                        yield text
                async for audio in super().stream(record(), turn_id=turn_id):
                    yield audio

        for split in range(1, len(marker)):
            speech = Speech()
            model = TextModel([marker[:split], marker[split:] + 'secret-arguments'])
            chat = ChatService(model, speech)
            turn = chat.create_turn(session_id='s', user_text='播放节目')
            output = [item async for item in chat.stream_turn(turn)]
            text = ''.join(item.text for item in output if isinstance(item, TextDelta))
            assert text and '回复出现异常' in text
            assert '<' not in text and 'DSML' not in text and 'secret-arguments' not in text
            assert ''.join(speech.texts) == text
            assert next(item.text for item in output if isinstance(item, TurnCompleted)) == text
            assert model.closed
            await chat.aclose()
        assert 'reply_protocol_blocked' in caplog.text
        assert 'secret-arguments' not in caplog.text
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())


def test_protocol_block_stops_upstream_without_executing_text_as_a_tool():
    async def scenario():
        class Model(TextModel):
            async def stream(self, messages, *, tools=()):
                try:
                    yield '我们换个话题。'
                    yield LEAK
                    raise AssertionError('must close stream after detecting protocol')
                finally:
                    self.closed = True

        model = Model([])
        chat = ChatService(model)
        turn = chat.create_turn(session_id='s', user_text='继续')
        text = ''.join([part async for part in chat.stream_reply(turn)])
        assert text.startswith('我们换个话题。')
        assert '回复出现异常' in text and 'DSML' not in text
        assert model.closed
        await chat.aclose()
    asyncio.run(scenario())


def test_ordinary_streaming_text_is_not_buffered_until_completion():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Model:
            async def stream(self, messages, *, tools=()):
                yield '正常的第一句话。'
                entered.set()
                await release.wait()
                yield '第二句里有 x < 3 和 DSML 这个普通单词。'

            async def aclose(self):
                pass

        chat = ChatService(Model())
        stream = chat.stream_reply(chat.create_turn(session_id='s', user_text='你好'))
        assert await anext(stream) == '正常的第一句话。'
        remaining = asyncio.create_task(anext(stream))
        await asyncio.wait_for(entered.wait(), 1)
        assert not remaining.done()
        release.set()
        assert await remaining == '第二句里有 x < 3 和 DSML 这个普通单词。'
        assert [text async for text in stream] == []
        await chat.aclose()
    asyncio.run(scenario())


def test_cancellation_discards_buffered_prefix_and_does_not_affect_another_turn():
    async def scenario():
        waiting, closed = asyncio.Event(), asyncio.Event()

        class Model:
            async def stream(self, messages, *, tools=()):
                if messages[-1].content.endswith('第一轮'):
                    try:
                        yield '<｜｜DS'
                        waiting.set()
                        await asyncio.Future()
                    finally:
                        closed.set()
                else:
                    yield '另一轮正常回复。'

            async def aclose(self):
                pass

        chat = ChatService(Model())

        async def collect(text):
            turn = chat.create_turn(session_id='s', user_text=text)
            return [item async for item in chat.stream_reply(turn)]

        first = asyncio.create_task(collect('第一轮'))
        await asyncio.wait_for(waiting.wait(), 1)
        assert await collect('第二轮') == ['另一轮正常回复。']
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert closed.is_set()
        await chat.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('suffix,blocked', [('<', False), ('</', False), ('<｜｜DS', True), ('<|DSM', True)])
def test_end_of_stream_flushes_ordinary_symbols_but_blocks_truncated_protocol(suffix, blocked):
    async def scenario():
        model = TextModel(['正常文字。', suffix])
        chat = ChatService(model)
        turn = chat.create_turn(session_id='s', user_text='你好')
        text = ''.join([item async for item in chat.stream_reply(turn)])
        if blocked:
            assert '回复出现异常' in text and suffix not in text
        else:
            assert text == '正常文字。' + suffix
        await chat.aclose()
    asyncio.run(scenario())


def test_timeout_closes_upstream_and_does_not_turn_cancellation_into_a_failure_reply():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        output = []

        class Model:
            async def stream(self, messages, *, tools=()):
                try:
                    yield '<｜｜DS'
                    entered.set()
                    await asyncio.Future()
                finally:
                    closed.set()

            async def aclose(self):
                pass

        chat = ChatService(Model())

        async def collect():
            async with asyncio.timeout(.1):
                async for text in chat.stream_reply(chat.create_turn(session_id='s', user_text='你好')):
                    output.append(text)

        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(TimeoutError):
            await task
        assert closed.is_set() and output == []
        await chat.aclose()
    asyncio.run(scenario())


def test_search_then_invalid_select_finishes_without_a_third_llm_or_media_submission(caplog):
    async def scenario():
        h = Harness()

        class Model:
            rounds = 0

            async def stream(self, messages, *, tools=()):
                self.rounds += 1
                if self.rounds == 1:
                    yield ModelToolCall('search', 'podcast_search', '{"query":"private-query","kind":"program"}')
                elif self.rounds == 2:
                    selection_id = json.loads(messages[-1].content)['selection_id']
                    yield ModelToolCall('invalid-select', 'podcast_control', json.dumps({
                        'action': 'select', 'index': 1, 'selection_id': selection_id}))
                else:
                    yield LEAK

            async def aclose(self):
                pass

        model = Model()
        chat = ChatService(model, tool_registry=ToolRegistry((PodcastSearchTool(h.service), PodcastControlTool(h.service))))
        turn = chat.create_turn(session_id='s', user_text='放点足球节目听听', device_id='device')
        try:
            text = ''.join([item async for item in chat.stream_reply(turn, device_control_enabled=True, media_commands=h.commands)])
            assert '听书操作没有完成' in text and 'DSML' not in text
            assert model.rounds == 2
            assert not any(event['type'] == 'media_control' for event in h.events)
            assert all(call[0] == 'search' for call in h.provider.calls)
            assert 'tool_execution_result' in caplog.text
            assert 'tool=podcast_control' in caplog.text and 'action=select' in caplog.text
            assert 'code=invalid_action' in caplog.text and 'round=2' in caplog.text
            assert f'turn_id={turn.turn_id}' in caplog.text and 'call_id=invalid-select' in caplog.text
            assert 'private-query' not in caplog.text and 'secret-arguments' not in caplog.text
        finally:
            await chat.aclose()
            await h.close()
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())


def test_accepted_media_task_is_not_reported_as_failed_if_reply_leaks_protocol():
    async def scenario():
        h = Harness()

        class Model:
            rounds = 0

            async def stream(self, messages, *, tools=()):
                self.rounds += 1
                if self.rounds == 1:
                    yield ModelToolCall('search', 'podcast_search', '{"query":"足球","kind":"program"}')
                elif self.rounds == 2:
                    yield ModelToolCall('play', 'podcast_control', json.dumps({
                        'action': 'play', 'index': 1, 'selection_id': json.loads(messages[-1].content)['selection_id']}))
                else:
                    assert json.loads(messages[-1].content)['status'] == 'accepted'
                    yield LEAK

            async def aclose(self):
                pass

        chat = ChatService(Model(), tool_registry=ToolRegistry((PodcastSearchTool(h.service), PodcastControlTool(h.service))))
        turn = chat.create_turn(session_id='s', user_text='放点足球节目听听', device_id='device')
        try:
            text = ''.join([item async for item in chat.stream_reply(turn, device_control_enabled=True, media_commands=h.commands)])
            assert '已提交' in text and 'DSML' not in text and '没有完成' not in text
            assert (await h.next('media_control'))['action'] == 'play'
        finally:
            await chat.aclose()
            await h.close()
    asyncio.run(scenario())
