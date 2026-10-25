"""Replay one saved text-only LLM request; never start Runtime, TTS, or memory jobs."""

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from pathlib import Path

from newtalk.chat import ChatMessage, ChatService, OpenAICompatibleChatModel
from newtalk.chat.context_budget import estimate_request_tokens
from newtalk.config import PROJECT_ROOT, load_config
from newtalk.context_debug import ContextDebugger


def load_request(path: Path, turn_id: str) -> dict:
    with path.open(encoding='utf-8') as source:
        for line in source:
            event = json.loads(line)
            if (event.get('turn_id') == turn_id and event.get('event') == 'llm_request'
                    and event.get('request_index') == 1):
                request = event['request']
                if set(request) not in ({'model', 'messages'}, {'model', 'messages', 'thinking'}) or any(
                    set(message) != {'role', 'content'} or message['role'] not in {'system', 'user', 'assistant'}
                    or not isinstance(message['content'], str) for message in request['messages']
                ):
                    raise ValueError('Probe only accepts a text-only request without tool calls or extra parameters')
                if 'thinking' in request and request['thinking'] not in ({'type': 'enabled'}, {'type': 'disabled'}):
                    raise ValueError('Probe requires a valid saved thinking toggle')
                return request
    raise ValueError('Saved request was not found; check turn ID and log rotation')


async def probe(request: dict, *, cycles: int, path: Path) -> None:
    config = load_config()
    if not config.llm_api_key or config.llm_model != request['model']:
        raise ValueError('Configured model must match the saved request')
    if estimate_request_tokens(request) > config.context_input_tokens:
        raise ValueError('Current budget would change the saved request')
    expected_hash = sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()
    messages = tuple(ChatMessage(**message) for message in request['messages'])
    debugger = ContextDebugger(enabled=True, path=path)
    try:
        for cycle in range(1, cycles + 1):
            # 新建的是 HTTP 客户端，不声称重启了供应商模型或清除了云端缓存。
            model = OpenAICompatibleChatModel(
                api_key=config.llm_api_key, base_url=config.llm_base_url, model=config.llm_model,
                timeout_seconds=config.llm_timeout_seconds, input_token_budget=config.context_input_tokens,
                thinking=request.get('thinking', {}).get('type', 'default'),
            )
            service = ChatService(model)
            try:
                for case in ('new_client', 'immediate_reuse'):
                    turn = service.create_turn(session_id='llm-probe', device_id='llm-probe',
                                               user_text='diagnostic replay', messages=messages)
                    token = debugger.activate(turn)
                    try:
                        debugger.record_turn(turn, 'llm_probe_case', cycle=cycle, case=case)
                        async with asyncio.timeout(90):
                            async for _ in model.stream(messages):
                                pass
                    finally:
                        debugger.reset(token)
                    summary = next(event for event in debugger.events_for_device('llm-probe', turn.turn_id)
                                   if event['event'] == 'llm_request_timing')
                    if summary['payload_sha256'] != expected_hash:
                        raise ValueError('Replay request changed; results must not be compared')
                    # 不打印完整输入、记忆或回答，只输出与时延有关的元数据。
                    print(json.dumps({'cycle': cycle, 'case': case, **summary}, ensure_ascii=False), flush=True)
            finally:
                await model.aclose()
    finally:
        debugger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turn-id', required=True)
    parser.add_argument('--log-path', type=Path, default=PROJECT_ROOT / 'logs/context-debug.jsonl')
    parser.add_argument('--cycles', type=int, choices=(1, 2, 3), default=2)
    parser.add_argument('--confirm-live', action='store_true', help='Acknowledge real API usage and cost')
    args = parser.parse_args()
    if not args.confirm_live:
        parser.error('Use --confirm-live to acknowledge real API calls (2 requests per cycle)')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = PROJECT_ROOT / f'logs/llm-latency-probe-{stamp}.jsonl'
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(probe(load_request(args.log_path, args.turn_id), cycles=args.cycles, path=path))
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'message': 'Probe failed; inspect local timing trace',
                          'trace_path': str(path)}))
        raise SystemExit(1) from None
    print(json.dumps({'trace_path': str(path)}))


if __name__ == '__main__':
    main()
