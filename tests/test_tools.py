import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass

import pytest

from newtalk.chat import ChatService, ModelToolCall
from newtalk.reminders.service import ReminderService
from newtalk.reminders.store import InMemoryReminderStore
from newtalk.tools import ToolContext, ToolRegistry
from newtalk.tools.builtin import create_tool_registry
from newtalk.tools.memory import MemorySearchTool
from tests.test_memory import RecordingMemoryProvider


def context(*, identity=None, client=False, mode='tool', device='device'):
    turn = ChatService().create_turn(session_id='session', user_text='当前问题',
                                     device_id=device, speaker_identity_id=identity)
    return ToolContext(turn, client, mode)


@dataclass(frozen=True, slots=True)
class Echo:
    value: str = 'echo'
    name = 'demo_echo'

    def definition(self, context):
        return {'type': 'function', 'function': {'name': self.name, 'parameters': {
            'type': 'object', 'properties': {}, 'additionalProperties': False,
        }}}

    async def execute(self, context, call):
        return self.value


@pytest.mark.parametrize('identity,enabled,mode,client,reminder,expected', [
    (None, True, 'tool', False, True, ()),
    ('member', True, 'tool', False, True, ('memory_search',)),
    ('member', False, 'tool', True, True, ('device_control', 'reminder_manage')),
    (None, True, 'tool', True, True, ('device_control', 'reminder_manage')),
    ('member', True, 'preload', True, True, ('device_control', 'reminder_manage')),
    ('member', True, 'tool', True, False, ('memory_search', 'device_control')),
])
def test_tool_selection_preserves_identity_memory_mode_and_client_boundaries(
    identity, enabled, mode, client, reminder, expected,
):
    provider = RecordingMemoryProvider()
    provider.enabled = enabled
    reminders = ReminderService(InMemoryReminderStore()) if reminder else None
    registry = create_tool_registry(MemorySearchTool(provider), reminders)
    selected = registry.select(context(identity=identity, client=client, mode=mode))
    assert selected.names == expected
    assert tuple(item['function']['name'] for item in selected.definitions) == expected
    assert provider.searches == []


def test_duplicate_names_and_mismatched_definitions_are_rejected():
    with pytest.raises(ValueError, match='unique'):
        ToolRegistry((Echo(), Echo()))

    class WrongName(Echo):
        def definition(self, context):
            return {'function': {'name': 'another_tool'}}

    with pytest.raises(ValueError, match='match'):
        ToolRegistry((WrongName(),)).select(context())


def test_selection_is_an_isolated_snapshot_and_cannot_execute_an_unoffered_tool():
    async def scenario():
        registry = ToolRegistry((Echo('old'),))
        selected = registry.select(context())
        definitions = selected.definitions
        definitions[0]['function']['name'] = 'forged'
        call = ModelToolCall('call', 'demo_echo', '{}')
        updated = registry.with_tool(Echo('new'))
        assert selected.names == ('demo_echo',)
        assert selected.definitions[0]['function']['name'] == 'demo_echo'
        assert await selected.execute(call) == 'old'
        assert await updated.select(context()).execute(call) == 'new'
        denied = await selected.execute(ModelToolCall('call', 'forged', '{}'))
        assert json.loads(denied)['code'] == 'unsupported_tool'

        provider = RecordingMemoryProvider()
        guest = create_tool_registry(MemorySearchTool(provider)).select(context(client=True))
        denied = await guest.execute(ModelToolCall('call', 'memory_search', '{"query":"秘密"}'))
        assert json.loads(denied)['code'] == 'unsupported_tool'
        assert provider.searches == []
    asyncio.run(scenario())


def test_custom_tool_can_be_added_without_changing_chat_service():
    class Model:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, *, tools=()):
            self.calls += 1
            if self.calls == 1:
                assert [item['function']['name'] for item in tools] == ['demo_echo']
                yield ModelToolCall('call', 'demo_echo', '{}', reasoning_content='真实推理')
            else:
                assert self.calls == 2
                assert tools == ()
                assert messages[-2].tool_calls[0].reasoning_content == '真实推理'
                assert messages[-1].tool_call_id == 'call'
                assert messages[-1].content == '执行成功'
                yield '根据工具结果回答'

        async def aclose(self):
            pass

    async def scenario():
        service = ChatService(Model(), tool_registry=ToolRegistry((Echo('执行成功'),)))
        turn = service.create_turn(session_id='s', user_text='调用新工具')
        assert [text async for text in service.stream_reply(turn)] == ['根据工具结果回答']
        await service.aclose()
    asyncio.run(scenario())


def test_shared_registry_keeps_concurrent_contexts_separate_and_propagates_cancellation():
    async def scenario():
        entered = {'a': asyncio.Event(), 'b': asyncio.Event()}
        release = {'a': asyncio.Event(), 'b': asyncio.Event()}
        closed = {'a': asyncio.Event(), 'b': asyncio.Event()}

        class Scoped(Echo):
            async def execute(self, context, call):
                device = context.turn.device_id
                entered[device].set()
                try:
                    await release[device].wait()
                    return device
                finally:
                    closed[device].set()

        registry = ToolRegistry((Scoped(),))
        call = ModelToolCall('call', 'demo_echo', '{}')
        a = asyncio.create_task(registry.select(context(device='a')).execute(call))
        b = asyncio.create_task(registry.select(context(device='b')).execute(call))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        assert closed['a'].is_set()
        assert not b.done()
        release['b'].set()
        assert await asyncio.wait_for(b, 1) == 'b'
        assert closed['b'].is_set()
    asyncio.run(scenario())


def test_tool_layer_does_not_swallow_provider_errors_or_allow_a_second_tool_execution():
    class Model:
        async def stream(self, messages, *, tools=()):
            yield ModelToolCall('call', 'demo_echo', '{}')

    async def scenario():
        calls = []

        class Once(Echo):
            async def execute(self, context, call):
                calls.append(call.call_id)
                return 'first'

        service = ChatService(Model(), tool_registry=ToolRegistry((Once(),)))
        turn = service.create_turn(session_id='s', user_text='执行')
        with pytest.raises(ValueError, match='limit'):
            _ = [text async for text in service.stream_reply(turn)]
        assert calls == ['call']

        class Failed(Echo):
            async def execute(self, context, call):
                raise RuntimeError('provider failed')

        service = ChatService(Model(), tool_registry=ToolRegistry((Failed(),)))
        with pytest.raises(RuntimeError, match='provider failed'):
            _ = [text async for text in service.stream_reply(turn)]
    asyncio.run(scenario())


def test_tool_modules_support_independent_import_order():
    for module in ('newtalk.tools.memory', 'newtalk.tools.reminder', 'newtalk.tools.builtin'):
        result = subprocess.run(
            [sys.executable, '-c', f'import {module}; from newtalk.chat import ChatService'],
            capture_output=True, text=True, encoding='utf-8', timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
