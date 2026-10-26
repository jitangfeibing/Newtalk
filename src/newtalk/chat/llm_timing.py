"""LLM 单次逻辑请求的被动计时；HTTP 重试共享请求，不能共享可变的全局计时器。"""

from contextvars import ContextVar
from hashlib import sha256
import json
import logging
import re
from time import perf_counter
from uuid import uuid4

import httpx

from newtalk.context_debug import record_context


logger = logging.getLogger(__name__)
active_request_timing: ContextVar['LlmRequestTiming | None'] = ContextVar('llm_request_timing', default=None)
_NETWORK_PHASES = {'connect_tcp', 'start_tls', 'send_request_headers', 'send_request_body',
                   'receive_response_headers'}


class LlmRequestTiming:
    def __init__(self, model: str) -> None:
        self.call_id = uuid4().hex
        self.model = model
        self.started_at = perf_counter()
        self.marks: dict[str, float] = {}
        self.attempts: list[dict] = []
        self.request_info: dict = {}
        self.usage: dict = {}

    def elapsed(self) -> float:
        return round((perf_counter() - self.started_at) * 1000, 3)

    def mark(self, name: str) -> None:
        if name not in self.marks:
            self.marks[name] = self.elapsed()

    def prepared(self, request: dict, *, input_tokens_estimate: int) -> None:
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        # 指纹只证明业务请求相同，不包含密钥；它也不证明云端缓存、随机性或调度相同。
        self.request_info = {'payload_sha256': sha256(payload).hexdigest(), 'request_bytes': len(payload),
                             'message_count': len(request['messages']),
                             'input_tokens_estimate': input_tokens_estimate}
        self.mark('prepared_ms')

    def observe_usage(self, chunk) -> None:
        usage = getattr(chunk, 'usage', None)
        for key in ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
            value = getattr(usage, key, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage[key] = value

    def finish(self, outcome: str) -> None:
        fields = {'call_id': self.call_id, 'model': self.model, 'outcome': outcome,
                  **self.request_info, **self.marks, 'attempts': self.attempts,
                  'usage': self.usage, 'total_ms': self.elapsed()}
        record_context('llm_request_timing', **fields)
        # 不记录 request/headers、异常正文或任何生成内容，普通日志仅存诊断元数据。
        logger.info('llm_request_timing call_id=%s model=%s outcome=%s metrics=%s',
                    self.call_id, self.model, outcome, json.dumps(fields, separators=(',', ':')))


async def observe_http_request(request: httpx.Request) -> None:
    timing = active_request_timing.get()
    if timing is None:
        return
    attempt = {'index': len(timing.attempts) + 1, 'request_start_ms': timing.elapsed(),
               'host': request.url.host, 'trace_seen': False, 'phases': []}
    retry_count = request.headers.get('x-stainless-retry-count', '')
    if len(retry_count) <= 4 and retry_count.isascii() and retry_count.isdecimal():
        attempt['sdk_retry_count'] = int(retry_count)
    timing.attempts.append(attempt)
    # 每次 SDK 重试都有独立 Request；回调闭包绑定自己的 attempt，避免并发串线。
    request.extensions['newtalk_llm_attempt'] = attempt

    async def trace(event: str, info: dict) -> None:
        del info  # 原始 trace info 含 Header、异常等敏感内容，绝不序列化。
        parts = event.split('.')
        if len(parts) < 3 or parts[-2] not in _NETWORK_PHASES:
            return
        attempt['trace_seen'] = True
        if len(attempt['phases']) < 48:
            attempt['phases'].append({'phase': parts[-2], 'event': parts[-1], 'at_ms': timing.elapsed()})

    request.extensions['trace'] = trace


async def observe_http_response(response: httpx.Response) -> None:
    timing = active_request_timing.get()
    attempt = response.request.extensions.get('newtalk_llm_attempt')
    if timing is None or attempt is None:
        return
    attempt['response_headers_ms'] = timing.elapsed()
    attempt['status_code'] = response.status_code
    request_id = response.headers.get('x-request-id') or response.headers.get('request-id')
    if request_id and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
        attempt['provider_request_id'] = request_id
