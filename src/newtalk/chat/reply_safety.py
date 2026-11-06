from collections.abc import AsyncIterator
from contextlib import aclosing
import json
import logging

from newtalk.chat.models import ModelToolCall

logger = logging.getLogger(__name__)
_MARKERS = tuple(
    f"<{closing}{bars}DSML{bars}"
    for closing in ("", "/") for bars in ("|", "||", "｜", "｜｜")
)
_MAX_MARKER_LENGTH = max(map(len, _MARKERS))


class _ProtocolLeak(ValueError):
    pass


class _TextGuard:
    def __init__(self) -> None:
        self.pending = ""

    def push(self, text: str) -> str:
        text = self.pending + text
        if any(marker in text for marker in _MARKERS):
            raise _ProtocolLeak
        # 只暂存可能跨流片段的协议前缀，普通文字不等待整轮完成。
        keep = max((size for size in range(1, min(len(text), _MAX_MARKER_LENGTH) + 1)
                    if any(marker.startswith(text[-size:]) for marker in _MARKERS)), default=0)
        self.pending = text[-keep:] if keep else ""
        return text[:-keep] if keep else text

    def finish(self) -> str:
        if len(self.pending) > 2 and self.pending.startswith(("<|", "<｜", "</|", "</｜")):
            raise _ProtocolLeak
        return self.pending


def protocol_failure_reply(last_result: str | None) -> str:
    try:
        payload = json.loads(last_result or "null")
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("status") == "accepted":
        return "操作请求已提交，请以页面显示的状态为准。"
    return "这次回复出现异常，请查看页面上的操作结果。"


async def guard_model_output(
    events: AsyncIterator[str | ModelToolCall], *, turn_id: str,
    round_index: int, fallback: str,
) -> AsyncIterator[str | ModelToolCall]:
    guard = _TextGuard()
    try:
        async with aclosing(events):
            async for event in events:
                if isinstance(event, str) and event:
                    safe = guard.push(event)
                    if safe:
                        yield safe
                else:
                    if guard.pending:
                        raise ValueError("Model mixed text with a tool call")
                    yield event
            tail = guard.finish()
            if tail:
                yield tail
    except _ProtocolLeak:
        # 不记录被拦截的正文/参数；它既不能送入 TTS，也不能被补解析执行。
        logger.warning("reply_protocol_blocked turn_id=%s round=%s code=dsml_in_content", turn_id, round_index)
        yield fallback
