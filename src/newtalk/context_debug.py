from __future__ import annotations

from contextvars import ContextVar, Token
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from newtalk.chat.models import Turn


logger = logging.getLogger(__name__)
DEFAULT_CONTEXT_LOG = Path(__file__).resolve().parents[2] / "logs" / "context-debug.jsonl"


class _PrivateFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # logging 默认会把失败的完整记录打印到 stderr，不能让私人上下文泄漏到普通日志。
        logger.warning("context_debug_write_failed")


@dataclass(frozen=True, slots=True)
class _TraceScope:
    debugger: "ContextDebugger"
    turn: Turn
    request_index: int = 0


_current_trace: ContextVar[_TraceScope | None] = ContextVar("context_trace", default=None)


class ContextDebugger:
    """显式开启后记录私有快照；看板只读有界进程缓存，不扫描磁盘历史。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        path: Path = DEFAULT_CONTEXT_LOG,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 2,
        max_cached_events: int = 500,
        max_cached_chars: int = 2_000_000,
        retention_seconds: float = 1800,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_cached_events <= 0 or max_cached_chars <= 0 or retention_seconds <= 0:
            raise ValueError("Context cache limits must be positive")
        self.enabled = enabled
        self.path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handler: _PrivateFileHandler | None = None
        # 不挂到 logging 的父子树，避免正文经 root/newtalk handler 进入终端或普通日志。
        self._logger = logging.Logger("context_debug_private", level=logging.INFO)
        self._logger.propagate = False
        self._closed = False
        self._events: deque[tuple[float, str]] = deque()
        self._cached_chars = 0
        self._max_cached_events = max_cached_events
        self._max_cached_chars = max_cached_chars
        self.retention_seconds = retention_seconds
        self._clock = clock

    def record(self, event: str, **fields: Any) -> None:
        if not self.enabled or self._closed:
            return
        try:
            text = json.dumps(
                {"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
                ensure_ascii=False, separators=(",", ":"),
            )
            self._events.append((self._clock(), text))
            self._cached_chars += len(text)
            self._trim_cache()
            if self._handler is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handler = _PrivateFileHandler(
                    self.path, maxBytes=self._max_bytes, backupCount=self._backup_count,
                    encoding="utf-8", delay=True,
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._handler = handler
                self._logger.addHandler(handler)
            self._logger.info(text)
        except Exception:
            # 观测失败不能改变聊天结果；这里只报告事件名，不输出 payload 或异常正文。
            logger.warning("context_debug_record_failed")

    def _trim_cache(self) -> None:
        cutoff = self._clock() - self.retention_seconds
        while self._events and (
            self._events[0][0] < cutoff
            or len(self._events) > self._max_cached_events
            or self._cached_chars > self._max_cached_chars
        ):
            _, text = self._events.popleft()
            self._cached_chars -= len(text)

    def events_for_device(self, device_id: str, turn_id: str | None = None) -> list[dict]:
        if not self.enabled or self._closed:
            return []
        self._trim_cache()
        # 每次重新解析，API 调用方不能修改用于后续取证的原始快照。
        events = (json.loads(text) for _, text in self._events)
        return [event for event in events
                if event.get("device_id") == device_id
                and (turn_id is None or event.get("turn_id") == turn_id)]

    def record_turn(self, turn: Turn, event: str, **fields: Any) -> None:
        self.record(
            event, turn_id=turn.turn_id, session_id=turn.session_id,
            device_id=turn.device_id, **fields,
        )

    def activate(self, turn: Turn) -> Token:
        # 每个 producer Task 继承自己的作用域；不能用实例字段保存“当前 Turn”。
        return _current_trace.set(_TraceScope(self, turn) if self.enabled else None)

    @staticmethod
    def reset(token: Token) -> None:
        _current_trace.reset(token)

    def close(self) -> None:
        self._closed = True
        self._events.clear()
        self._cached_chars = 0
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            try:
                self._handler.close()
            except Exception:
                logger.warning("context_debug_close_failed")
            self._handler = None


def record_context(event: str, **fields: Any) -> None:
    scope = _current_trace.get()
    if scope is not None:
        scope.debugger.record_turn(scope.turn, event, **fields)


def record_model_request(
    request: dict[str, Any], *, budget: dict[str, Any] | None = None, call_id: str | None = None,
) -> None:
    scope = _current_trace.get()
    if scope is None:
        return
    scope = replace(scope, request_index=scope.request_index + 1)
    _current_trace.set(scope)
    # 此处只接收最终消息/工具请求，不接收 Client、HTTP Header、Key 或环境配置。
    scope.debugger.record_turn(
        scope.turn, "llm_request", request_index=scope.request_index, request=request,
        **({"budget": budget} if budget is not None else {}),
        **({"call_id": call_id} if call_id is not None else {}),
    )
