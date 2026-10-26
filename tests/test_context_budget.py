import asyncio
import json
from types import SimpleNamespace

import pytest

from newtalk.chat import ChatMessage, OpenAICompatibleChatModel, ModelToolCall
from tests.test_openai_compatible import StubClient, StubStream
from tests.test_websocket import RecordingModel, make_client, receive_turn
from newtalk.chat import ChatService
from newtalk.chat.context_budget import ContextBudgetExceeded, estimate_request_tokens, fit_request
from newtalk.context_debug import ContextDebugger
from tests.test_transport_runtime import RecordingWebSocket, make_runtime


def test_language_learning_original_input_survives_more_than_eight_turns_and_reconnect():
    model = RecordingModel()
    client = make_client(chat_service=ChatService(model))
    with client.websocket_connect('/ws') as websocket:
        websocket.receive_json()
        for index in range(12):
            text = '我想练习英语，请一句一句来' if index == 0 else f'继续练习第{index}句'
            websocket.send_json({'type': 'text_input', 'event_id': f'learn-{index}', 'text': text})
            assert receive_turn(websocket)[2]['type'] == 'turn_completed'
        assert any('我想练习英语' in message.content for message in model.requests[-1])
    with client.websocket_connect('/ws') as websocket:
        snapshot = websocket.receive_json()['dialogue']
        assert len(snapshot['items']) == 12
        assert snapshot['items'][0]['user_text'] == '我想练习英语，请一句一句来'


def test_full_request_budget_trims_history_not_system_profile_or_current_input():
    async def scenario():
        client = StubClient(StubStream([SimpleNamespace(type='content.delta', delta='继续')]))
        model = OpenAICompatibleChatModel(api_key='unused', model='test', system_prompt='角色规则', client=client)
        model._input_token_budget = 512
        messages = (
            ChatMessage('system', '[当前说话人画像] 喜欢英语'),
            ChatMessage('user', 'old-' + 'a' * 350),
            ChatMessage('assistant', 'old-answer-' + 'b' * 350),
            ChatMessage('user', '继续英语练习'),
        )
        assert [chunk async for chunk in model.stream(messages)] == ['继续']
        request = client.chat.completions.request
        assert request['messages'] == [
            {'role': 'system', 'content': '角色规则'},
            {'role': 'system', 'content': '[当前说话人画像] 喜欢英语'},
            {'role': 'user', 'content': '继续英语练习'},
        ]
    asyncio.run(scenario())


def test_second_request_budget_keeps_tool_pair_and_reports_truncated_result():
    async def scenario():
        client = StubClient(StubStream([SimpleNamespace(type='content.delta', delta='请确认是哪次经历')]))
        model = OpenAICompatibleChatModel(api_key='unused', model='test', client=client)
        model._input_token_budget = 700
        call = ModelToolCall('call-1', 'memory_search', '{"query":"英语练习"}')
        messages = (
            ChatMessage('user', '回忆一下英语练习'),
            ChatMessage('assistant', '', tool_calls=(call,)),
            ChatMessage('tool', json.dumps({'status': 'ok', 'memories': ['a' * 3000]}), tool_call_id=call.call_id),
        )
        _ = [chunk async for chunk in model.stream(messages)]
        sent = client.chat.completions.request['messages']
        assert sent[0]['content'] == '回忆一下英语练习'
        assert sent[1]['tool_calls'][0]['id'] == 'call-1'
        assert sent[2]['tool_call_id'] == 'call-1'
        assert json.loads(sent[2]['content'])['status'] == 'truncated'
        assert len(sent[2]['content']) < 500
        assert json.loads(messages[-1].content)['status'] == 'ok'
    asyncio.run(scenario())


def test_budget_rejection_sends_no_request_and_next_turn_can_recover(tmp_path):
    async def scenario():
        # 正常请求现在包含设备工具定义，预算仍必须覆盖每次完整请求。
        input_budget = 2048
        client = StubClient(StubStream([SimpleNamespace(type='content.delta', delta='继续聊天')]))
        model = OpenAICompatibleChatModel(api_key='unused', model='test', client=client, input_token_budget=input_budget)
        websocket = RecordingWebSocket()
        runtime = make_runtime(websocket)
        path = tmp_path / 'context.jsonl'
        runtime._chat_service = ChatService(model, context_debug=ContextDebugger(enabled=True, path=path))
        await runtime.start()
        try:
            await runtime.start_turn(text='很长的输入' * 500, event_id='large', identity=None)
            first_id = runtime.active_turn_id
            await asyncio.wait_for(runtime._turn_state.task, 2)
            assert client.chat.completions.request is None
            failure = websocket.json_events[-1]
            assert failure['type'] == 'turn_failed'
            assert failure['code'] == 'context_budget_exceeded'
            await runtime.start_turn(text='继续聊天', event_id='small', identity=None)
            await asyncio.wait_for(runtime._turn_state.task, 2)
            assert client.chat.completions.request['messages'] == [
                {'role': 'user', 'content': '[说话人：Guest（未识别访客）]\n继续聊天'},
            ]
            assert len(runtime._dialogue.exchanges) == 2
            assert runtime._dialogue.exchanges[0].assistant_text == ''
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()
        stored = runtime._dialogue_store._history[runtime.session_id]
        assert len(stored) == 2
        assert stored[0].exchange.turn_id == first_id
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        assert len([event for event in events if event['event'] == 'context_budget_rejected']) == 1
        sent = next(event for event in events if event['event'] == 'llm_request')
        assert sent['budget']['omitted_history_turn_ids'] == [first_id]
        assert sent['budget']['input_token_limit'] == input_budget
        assert sent['budget']['after_tokens_estimate'] <= input_budget
        assert sent['request']['tools'][0]['function']['name'] == 'device_control'
    asyncio.run(scenario())


def test_budget_counts_tool_definitions_and_does_not_cut_current_user_or_system():
    request = {'model': 'test', 'messages': [
        {'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'},
    ], 'tools': [{'type': 'function', 'function': {'name': 'tool', 'description': 'x' * 1000}}]}
    with pytest.raises(ContextBudgetExceeded) as failure:
        fit_request(request, limit=256, source_turn_ids=[None, None])
    assert failure.value.decision['components']['request_and_tool_definitions'] > 1000
    assert request['messages'][0]['content'] == 'system'
    assert request['messages'][-1]['content'] == 'current'


def test_budget_preserves_message_groups_metadata_and_exact_boundary():
    request = {'model': 'test', 'messages': [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'old'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': 'interrupted user input'},
        {'role': 'user', 'content': 'current'},
    ]}
    sources = [None, 'old-turn', 'old-turn', 'interrupted-turn', None]
    cost = estimate_request_tokens(request)
    same, decision = fit_request(request, limit=cost, source_turn_ids=sources)
    assert same == request
    assert decision['is_estimate'] is True
    assert decision['included_history_turn_ids'] == ['old-turn', 'interrupted-turn']
    assert sum(decision['components'].values()) == cost
    trimmed, decision = fit_request(request, limit=cost - 1, source_turn_ids=sources)
    assert trimmed['messages'] == [request['messages'][0], *request['messages'][3:]]
    assert decision['omitted_history_turn_ids'] == ['old-turn']
    assert decision['included_history_turn_ids'] == ['interrupted-turn']
    assert estimate_request_tokens(trimmed) == decision['after_tokens_estimate']
    assert len(request['messages']) == 5


def test_preloaded_memory_is_budgeted_as_data_and_not_mistaken_for_a_historical_user():
    from newtalk.chat.prompts import MEMORY_CONTEXT_PREFIX
    request = {'model': 'test', 'messages': [
        {'role': 'user', 'content': 'old goal'},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': MEMORY_CONTEXT_PREFIX + json.dumps({'status': 'ok', 'memories': ['x' * 3000]})},
        {'role': 'user', 'content': 'current'},
    ]}
    fitted, decision = fit_request(request, limit=800,
                                  source_turn_ids=['old', 'old', None, None], memory_context_indices={2})
    assert decision['memory_context_truncated'] is True
    assert decision['included_history_groups'] == 1
    assert decision['included_history_turn_ids'] == ['old']
    assert decision['omitted_history_turn_ids'] == []
    assert decision['components']['preloaded_memory'] > 0
    assert sum(decision['components'].values()) == estimate_request_tokens(fitted)
    assert json.loads(fitted['messages'][2]['content'].split('\n', 1)[1])['status'] == 'truncated'
    assert fitted['messages'][-1]['content'] == 'current'
