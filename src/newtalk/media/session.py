from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol, runtime_checkable
from uuid import uuid4

from newtalk.media.models import (
    CLIENT_MEDIA_ACTIONS,
    MEDIA_RESOLVE_ACTIONS,
    MediaControl,
    MediaIntent,
    MediaObservation,
    MediaUnavailable,
)

logger = logging.getLogger(__name__)
_MAX_TASKS = 8
_MAX_PENDING = 16
_ACK_SECONDS = 90.0
_STATUSES = {"waiting", "playing", "paused", "stopped", "ended", "failed", "no_track"}


class MediaResolver(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def command(self, device_id: str, action: str, query: str | None,
                      index: int | None, call_id: str) -> MediaControl: ...


@runtime_checkable
class CatalogueResolver(Protocol):
    @property
    def enabled(self) -> bool: ...
    async def search(self, query: str, kind: str, can_commit: Callable[[], bool]) -> dict: ...
    def facts(self) -> dict: ...
    async def resolve(self, intent: MediaIntent, operation_id: str) -> MediaControl: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnMediaCommands:
    session: MediaSession
    can_submit: Callable[[], bool]

    def submit(self, intent: MediaIntent) -> dict:
        if not self.can_submit():
            raise MediaUnavailable('原对话已取消，未接受新的媒体操作。', code='cancelled')
        return self.session.submit(intent)

    @property
    def active_source(self) -> str:
        return self.session.active_source

    def catalogue(self, source: str) -> dict:
        return self.session.catalogue(source)

    async def discover(self, source: str, query: str, kind: str) -> dict:
        if not self.can_submit():
            raise MediaUnavailable('原对话已取消。', code='cancelled')
        return await self.session.discover(source, query, kind, self.can_submit)


class MediaSession:
    """连接拥有媒体操作；聊天轮只提交意图，不拥有解析任务。"""

    def __init__(self, *, device_id: str, resolvers: Mapping[str, MediaResolver | CatalogueResolver],
                 emit: Callable[[dict], Awaitable[None]], resolve_timeout: float = 25.0) -> None:
        self.device_id = device_id
        self._resolvers = dict(resolvers)
        self._emit = emit
        self._resolve_timeout = resolve_timeout
        self._tasks: set[asyncio.Task] = set()
        self._resolver_task: asyncio.Task | None = None
        self._pending: dict[str, tuple[str, float, str | None]] = {}
        self._last_command: tuple[str, str] | None = None
        self._seen: OrderedDict[str, tuple[MediaIntent, dict]] = OrderedDict()
        self._revision = 0
        self._closing = False
        self._observation = MediaObservation('', 'none', None, None)

    @property
    def enabled(self) -> bool:
        return not self._closing and any(item.enabled for item in self._resolvers.values())

    @property
    def observation(self) -> MediaObservation:
        return self._observation

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def pending_requests(self) -> tuple[str, ...]:
        return tuple(self._pending)

    @property
    def resolving(self) -> bool:
        return self._observation.operation_state == 'resolving'

    def for_turn(self, can_submit: Callable[[], bool]) -> TurnMediaCommands:
        return TurnMediaCommands(self, can_submit)

    @property
    def active_source(self) -> str:
        return self._observation.source

    def catalogue(self, source: str) -> dict:
        resolver = self._resolvers.get(source)
        return resolver.facts() if isinstance(resolver, CatalogueResolver) and resolver.enabled else {}

    def prompt(self) -> str:
        return self._observation.prompt(catalogues={name: self.catalogue(name) for name in self._resolvers
                                                   if isinstance(self._resolvers[name], CatalogueResolver)})

    async def discover(self, source: str, query: str, kind: str, can_commit: Callable[[], bool]) -> dict:
        resolver = self._resolvers.get(source)
        if self._closing or not isinstance(resolver, CatalogueResolver) or not resolver.enabled:
            raise MediaUnavailable('内容搜索尚未启用。', code='unavailable')
        return await resolver.search(query, kind, lambda: not self._closing and can_commit())

    def submit(self, intent: MediaIntent) -> dict:
        source = intent.source if intent.action in MEDIA_RESOLVE_ACTIONS or self._observation.action == 'none' else self.active_source
        resolver = self._resolvers.get(source)
        if self._closing or resolver is None or not resolver.enabled:
            raise MediaUnavailable('媒体服务未启用或连接已关闭。', code='unavailable')
        self._validate(intent)
        previous = self._seen.get(intent.call_id)
        if previous is not None:
            if previous[0] != intent:
                raise ValueError('同一媒体请求编号不能用于不同操作。')
            return dict(previous[1])
        if len(self._tasks) >= _MAX_TASKS:
            raise MediaUnavailable('媒体请求繁忙，请稍后重试。', code='busy')
        if intent.action in {*MEDIA_RESOLVE_ACTIONS, 'stop'}:
            self._revision += 1
            if self._resolver_task is not None:
                self._resolver_task.cancel()
            operation_id = str(uuid4())
        else:
            operation_id = self._observation.operation_id or str(uuid4())
        self._observation = replace(self._observation, action=intent.action, operation_id=operation_id,
                                    desired_playing=intent.action in {*MEDIA_RESOLVE_ACTIONS, 'resume'},
                                    source=source, error_code=None, message=None)
        revision = self._revision
        if intent.action in MEDIA_RESOLVE_ACTIONS:
            target = intent.target or intent.query or ({'next': '下一集', 'change': '换个专辑'}.get(intent.action)
                     or (f'搜索结果第 {intent.index} 项' if intent.index else '轻音乐'))
            self._observation = replace(self._observation, operation_state='resolving', target=target)
            self._resolver_task = self._spawn(self._resolve(intent, resolver, revision, operation_id))
        elif intent.action == 'stop':
            self._observation = replace(self._observation, operation_state='stopped', target=None)
            self._spawn(self._control(MediaControl('stop', intent.call_id), revision))
        elif self.resolving:
            # 暂停只改变期望播放状态；继续时仍复用当前解析，不播放旧曲目。
            if intent.action == 'pause' and self._observation.track is not None:
                self._spawn(self._control(MediaControl('pause', intent.call_id), revision))
            else:
                self._spawn(self._emit_status(revision))
        else:
            self._spawn(self._control(MediaControl(intent.action, intent.call_id), revision))
        result = {'status': 'accepted', 'operation_id': operation_id, 'action': intent.action,
                  'operation_state': self._observation.operation_state, 'target': self._observation.target,
                  'message': '操作已接受；请简短确认正在处理，不要在网页反馈前宣称已播放。'}
        self._seen[intent.call_id] = (intent, result)
        while len(self._seen) > 128:
            self._seen.popitem(last=False)
        logger.info('media_operation_accepted device_id=%s operation_id=%s call_id=%s action=%s revision=%s',
                    self.device_id, operation_id, intent.call_id, intent.action, revision)
        return dict(result)

    @staticmethod
    def _validate(intent: MediaIntent) -> None:
        if intent.action not in (*CLIENT_MEDIA_ACTIONS, *MEDIA_RESOLVE_ACTIONS) or not isinstance(intent.call_id, str) or not intent.call_id:
            raise ValueError('媒体操作参数无效。')
        if intent.query is not None and (not isinstance(intent.query, str) or len(intent.query) > 80):
            raise ValueError('歌手或歌名须为至多 80 字的文字。')
        if intent.index is not None and (type(intent.index) is not int or not 1 <= intent.index <= 100):
            raise ValueError('歌曲序号无效。')
        if intent.action != 'play' and (intent.query is not None or intent.index is not None):
            raise ValueError('暂停、继续和停止无需歌曲参数。')
        if intent.selection_id is not None and (not isinstance(intent.selection_id, str) or len(intent.selection_id) > 80):
            raise ValueError('内容选择编号无效。')
        if intent.target is not None and (not isinstance(intent.target, str) or len(intent.target) > 200):
            raise ValueError('内容目标无效。')

    def _spawn(self, work: Awaitable) -> asyncio.Task:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error('media_task_failed device_id=%s error_type=%s', self.device_id, type(error).__name__)

    def _current(self, revision: int) -> bool:
        return not self._closing and revision == self._revision

    async def _resolve(self, intent: MediaIntent, resolver: MediaResolver | CatalogueResolver, revision: int, operation_id: str) -> None:
        started = monotonic()
        try:
            await self._emit_status(revision)
            async with asyncio.timeout(self._resolve_timeout):
                if isinstance(resolver, CatalogueResolver):
                    result = await resolver.resolve(intent, operation_id)
                else:
                    result = await resolver.command(self.device_id, intent.action, intent.query, intent.index, operation_id)
            if not self._current(revision):
                logger.info('media_result_discarded operation_id=%s reason=stale', operation_id)
                return
            if not isinstance(result, MediaControl) or result.track is None or result.action != 'play':
                raise MediaUnavailable('媒体源未返回有效资源。', code='invalid_response')
            self._observation = replace(self._observation, operation_state='ready', track=result.track,
                                        client_status=None)
            logger.info('media_resource_ready operation_id=%s source=%s title=%r artist=%r elapsed_ms=%.1f',
                        operation_id, intent.source, result.track.title, result.track.artist,
                        (monotonic() - started) * 1000)
            await self.publish_control(replace(result, autoplay=self._observation.desired_playing), revision=revision)
            await self._emit_status(revision)
        except asyncio.CancelledError:
            logger.info('media_operation_cancelled operation_id=%s elapsed_ms=%.1f', operation_id,
                        (monotonic() - started) * 1000)
            raise
        except (MediaUnavailable, ValueError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, MediaUnavailable) else ('timeout' if isinstance(exc, TimeoutError) else 'invalid_request')
            message = str(exc) or '媒体解析超时，请重试。'
            await self._fail(revision, operation_id, code, message, started)
        # 独立任务的边界必须报告失败；仅记录异常类型，避免泄漏供应商票据。
        except Exception as exc:  # noqa: BLE001
            await self._fail(revision, operation_id, 'internal_error', '媒体处理失败，请重新点播。', started,
                             error_type=type(exc).__name__)
        finally:
            if asyncio.current_task() is self._resolver_task:
                self._resolver_task = None

    async def _fail(self, revision: int, operation_id: str, code: str, message: str,
                    started: float, *, error_type: str = '', stage: str = 'resolve') -> None:
        if not self._current(revision):
            return
        self._observation = replace(self._observation, operation_state='failed', error_code=code, message=message)
        logger.warning('media_operation_failed operation_id=%s stage=%s code=%s error_type=%s elapsed_ms=%.1f',
                       operation_id, stage, code, error_type, (monotonic() - started) * 1000)
        await self._emit({'type': 'media_error', 'operation_id': operation_id, 'revision': revision,
                          'code': code, 'message': message})
        await self._emit_status(revision)

    async def _control(self, command: MediaControl, revision: int) -> None:
        started = monotonic()
        operation_id = self._observation.operation_id
        try:
            await self.publish_control(command, revision=revision)
            await self._emit_status(revision)
        except MediaUnavailable as exc:
            await self._fail(revision, operation_id, exc.code, str(exc), started, stage='control')

    async def publish_control(self, command: MediaControl, *, revision: int | None = None) -> None:
        if revision is None:
            self._revision += 1
            revision = self._revision
            if self._resolver_task is not None:
                self._resolver_task.cancel()
            self._observation = replace(self._observation, operation_id=str(uuid4()),
                                        operation_state='ready' if command.track else 'idle', target=None,
                                        source=command.track.media_type if command.track else self.active_source,
                                        desired_playing=command.action in {'play', 'resume'})
        if not self._current(revision):
            return
        now = monotonic()
        self._pending = {key: value for key, value in self._pending.items() if value[1] > now}
        if len(self._pending) >= _MAX_PENDING:
            raise MediaUnavailable('等待网页播放反馈，请稍后再试。', code='busy')
        request_id = str(uuid4())
        self._pending[request_id] = (command.action, now + _ACK_SECONDS, self._observation.operation_id)
        self._last_command = (request_id, command.action)
        self._observation = replace(self._observation, request_id=request_id, action=command.action,
                                    client_status=None, track=command.track or self._observation.track)
        await self._emit({'type': 'media_control', 'request_id': request_id, 'action': command.action,
                          'operation_id': self._observation.operation_id, 'revision': revision,
                          'track': command.track.payload() if command.track else None,
                          'autoplay': command.autoplay})

    async def _emit_status(self, revision: int) -> None:
        if self._current(revision):
            await self._emit({'type': 'media_status', **self._observation.facts(), 'revision': revision})

    def report(self, event: dict) -> bool:
        request_id, status = event.get('request_id'), event.get('status')
        pending = self._pending.get(request_id) if isinstance(request_id, str) else None
        if (pending is None or pending[1] <= monotonic() or event.get('action') != pending[0]
                or not isinstance(status, str) or status not in _STATUSES):
            return False
        if status == 'waiting':
            self._pending[request_id] = (pending[0], monotonic() + _ACK_SECONDS, pending[2])
        else:
            self._pending.pop(request_id, None)
        if self._last_command == (request_id, pending[0]):
            self._apply_status(status, pending[2])
        logger.info('media_command_result request_id=%s operation_id=%s action=%s status=%s',
                    request_id, pending[2], pending[0], status)
        return True

    def report_state(self, event: dict) -> bool:
        status = event.get('status')
        if (self._closing or self._last_command != (event.get('request_id'), event.get('action'))
                or not isinstance(status, str) or status not in _STATUSES - {'waiting', 'no_track'}):
            return False
        self._apply_status(status, self._observation.operation_id)
        logger.info('media_player_state request_id=%s status=%s', event.get('request_id'), status)
        return True

    def _apply_status(self, status: str, operation_id: str | None) -> None:
        changes = {'client_status': status}
        if not self.resolving and operation_id == self._observation.operation_id:
            if status in {'playing', 'ended', 'stopped'}:
                changes['operation_state'] = 'completed'
            elif status in {'failed', 'no_track'}:
                changes.update(operation_state='failed', error_code='player_failed',
                               message='网页没有开始播放，请重试或重新点播。')
        self._observation = replace(self._observation, **changes)

    async def close(self) -> None:
        self._closing = True
        self._revision += 1
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._pending.clear()
        self._seen.clear()
        for resolver in self._resolvers.values():
            if isinstance(resolver, CatalogueResolver):
                await resolver.close()
        self._last_command = None
        self._observation = MediaObservation('', 'none', None, None)
