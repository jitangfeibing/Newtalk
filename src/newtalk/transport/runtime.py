import asyncio
from collections import deque
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
import logging
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from fastapi import WebSocket

from newtalk.asr import AsrFinal, AsrPartial, SpeechRecognizer
from newtalk.audio import (
    INPUT_AUDIO_FORMAT,
    AudioInputSession,
    SpeechBoundary,
    VoiceActivityDetector,
    pcm_s16le_to_wav,
)
from newtalk.chat import (
    AudioCompleted,
    AudioFailed,
    AudioFrame,
    AudioStarted,
    ChatService,
    ChatMessage,
    DialogueExchange,
    DialogueSnapshot,
    DialogueStore,
    DialogueCacheCoordinator,
    DialogueSession,
    TextDelta,
    Turn,
    TurnCompleted,
)
from newtalk.identity import Identity, IdentityNotFoundError, IdentityService
from newtalk.memory import MemoryWriter
from newtalk.profile import SessionProfileCache
from newtalk.voiceprint import (
    VoicePrintClient,
    VoicePrintError,
    VoicePrintIdentification,
    VoicePrintUnavailableError,
)
from newtalk.chat.context_budget import ContextBudgetExceeded
from newtalk.chat.device_control import DEVICE_ACTIONS, DeviceControl, control_from_text
from newtalk.reminders.service import ReminderService
from newtalk.media.models import MediaControl, MediaIntent, MediaUnavailable
from newtalk.media.session import MediaSession
from newtalk.media.interaction import playback_stop_from_text
from newtalk.music.service import MusicService
from newtalk.podcasts.service import PodcastService


logger = logging.getLogger(__name__)
_SEND_END = object()
_RECENT_EVENT_LIMIT = 4096
# 输入落库独立于回复取消；数据库变慢时必须有界，满载明确拒绝新输入。
_MAX_PENDING_INPUT_WRITES = 128
_DEVICE_CONTROL_TIMEOUT_SECONDS = 5.0
_MAX_PENDING_DEVICE_CONTROLS = 16


@dataclass(frozen=True, slots=True)
class _PendingDeviceControl:
    action: str
    result: asyncio.Future[str]
    task: asyncio.Task[None]


def _format_optional_ms(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "none"


@dataclass(slots=True)
class _OutboundFrame:
    payload: dict[str, Any] | bytes | object
    turn_id: str | None
    delivered: asyncio.Future[None]


class _OutboundSender:
    """Serializes WebSocket writes and propagates terminal send failures."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        session_id: str,
        active_turn_id: Callable[[], str | None],
    ) -> None:
        self._websocket = websocket
        self._session_id = session_id
        self._active_turn_id = active_turn_id
        self._queue: asyncio.Queue[_OutboundFrame] = asyncio.Queue(maxsize=256)
        self._task: asyncio.Task[None] | None = None
        self._terminal_error: Exception | None = None

    def start(self) -> None:
        self._terminal_error = None
        self._task = asyncio.create_task(self._run())

    async def send(
        self,
        payload: dict[str, Any] | bytes | object,
        *,
        turn_id: str | None,
    ) -> None:
        task = self._task
        if task is None:
            raise RuntimeError("Connection sender has not started")
        if task.done():
            raise self._terminal_error or RuntimeError("Connection sender stopped")
        delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        frame = _OutboundFrame(payload, turn_id, delivered)
        put_task = asyncio.create_task(self._queue.put(frame))
        try:
            done, _ = await asyncio.wait(
                (put_task, task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done and not put_task.done():
                put_task.cancel()
                await asyncio.gather(put_task, return_exceptions=True)
                raise self._terminal_error or RuntimeError(
                    "Connection sender stopped"
                )
            await put_task
        except BaseException:
            if not put_task.done():
                put_task.cancel()
                await asyncio.gather(put_task, return_exceptions=True)
            raise

        # sender 可能在 queue.put 与此检查之间结束，并已完成 finally 清理。
        if task.done() and not delivered.done():
            delivered.set_exception(
                self._terminal_error or RuntimeError("Connection sender stopped")
            )
        await delivered

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            with suppress(Exception):
                await self.send(_SEND_END, turn_id=None)
        with suppress(Exception):
            await task

    async def _run(self) -> None:
        # 运行于：发送 Task；持锁：否；可能阻塞：等待队列及 WebSocket 写入，未设本地总超时。
        failure: Exception | None = None
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item.payload is _SEND_END:
                        if not item.delivered.done():
                            item.delivered.set_result(None)
                        return
                    if (
                        item.turn_id is not None
                        and item.turn_id != self._active_turn_id()
                    ):
                        if not item.delivered.done():
                            item.delivered.set_result(None)
                        continue
                    if isinstance(item.payload, bytes):
                        await self._websocket.send_bytes(item.payload)
                    elif isinstance(item.payload, dict):
                        await self._websocket.send_json(item.payload)
                    if not item.delivered.done():
                        item.delivered.set_result(None)
                except Exception as exc:
                    if not item.delivered.done():
                        item.delivered.set_exception(exc)
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
            logger.warning(
                "websocket_sender_failed session_id=%s error_type=%s",
                self._session_id,
                type(exc).__name__,
                exc_info=True,
            )
        finally:
            error = failure or RuntimeError("Connection sender stopped")
            self._terminal_error = error
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if not pending.delivered.done():
                    pending.delivered.set_exception(error)


@dataclass(frozen=True, slots=True)
class _TurnCancellation:
    task: asyncio.Task[None] | None
    turn_id: str | None
    stream_id: str | None
    playback_turn_id: str | None
    playback_stream_id: str | None


@dataclass(slots=True)
class _TurnState:
    task: asyncio.Task[None] | None = None
    turn_id: str | None = None
    stream_id: str | None = None
    playback_turn_id: str | None = None
    playback_stream_id: str | None = None

    def activate(self, turn_id: str) -> None:
        self.task = None
        self.turn_id = turn_id
        self.stream_id = None

    def attach_task(self, turn_id: str, task: asyncio.Task[None]) -> None:
        if self.turn_id != turn_id:
            task.cancel()
            raise RuntimeError("Cannot attach a task to an inactive Turn")
        self.task = task

    def begin_playback(self, turn_id: str, stream_id: str) -> bool:
        if self.turn_id != turn_id:
            return False
        self.stream_id = stream_id
        self.playback_turn_id = turn_id
        self.playback_stream_id = stream_id
        return True

    def clear_playback(self, turn_id: str, stream_id: str) -> bool:
        if (
            self.playback_turn_id != turn_id
            or self.playback_stream_id != stream_id
        ):
            return False
        self.playback_turn_id = None
        self.playback_stream_id = None
        return True

    def clear_active(self, turn_id: str) -> bool:
        if self.turn_id != turn_id:
            return False
        self.task = None
        self.turn_id = None
        self.stream_id = None
        return True

    def take_for_cancellation(self) -> _TurnCancellation:
        snapshot = _TurnCancellation(
            task=self.task,
            turn_id=self.turn_id,
            stream_id=self.stream_id,
            playback_turn_id=self.playback_turn_id,
            playback_stream_id=self.playback_stream_id,
        )
        self.task = None
        self.turn_id = None
        self.stream_id = None
        self.playback_turn_id = None
        self.playback_stream_id = None
        return snapshot


@dataclass(slots=True)
class _VoiceJoinState:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[VoicePrintIdentification] | None = None
    failure_reason: str | None = None
    started_at: float = field(default_factory=perf_counter)
    speech_ended_at: float | None = None
    asr_final_at: float | None = None
    vad_silence_ms: float | None = None
    media_only: bool = False
    media_request: tuple[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class _SpeakerResolution:
    identity: Identity | None
    score: float
    reason: str
    provider_elapsed_ms: float | None
    elapsed_ms: float
    timing: tuple[tuple[str, float | None], ...] = ()


@dataclass(frozen=True, slots=True)
class _AcceptedUserInput:
    revision: int
    persistence: asyncio.Task[str]


class ConnectionRuntime:
    """管理一条 WebSocket 连接的短期状态和并发任务。

    执行上下文：
    - 接收循环 Task（websocket._receive_messages）：start_text_turn、clear_dialogue、
      start_audio_input、push_audio、stop_audio_input、playback_drained；
      经 push_audio/stop_audio_input 调用 _on_speech_boundary、_on_utterance_audio，
      清空或语音开始时调用 cancel_turn，队列溢出时还会调用 _on_asr_error。
    - 识别 Task（AudioInputSession._recognize）：_on_asr_event、_on_asr_error；
      _on_asr_event 经 _resolve_voice_speaker 确认身份后调用 start_turn。
    - Turn Task：_stream_turn、_can_commit_turn，负责流式交付和完成后的提交。
    - 独立输入落库 Task：_persist_user_input、_can_commit_turn；不随 Turn 取消，正常关闭时收尾。
    - 发送 Task：_OutboundSender._run，串行写入 WebSocket，并过滤失效 Turn 输出。
    - websocket_endpoint Task：构造并 start；其 finally 中 close，等待各后台任务退出。
      接收循环是该 Task 中被 await 的协程，不是另一个独立创建的 Task。

    _turn_transition_lock 串行保护 start_turn/cancel_turn 的替换与取消：
    摘除旧 Turn 状态、等待其 Task 退出、发送通知，以及激活新 Turn 并保存 Task 引用。
    _cancel_turn 由已持锁的调用方执行；_stream_turn、播放回执及声纹汇合不持此锁。
    _dialogue_write_scope.lock 则按设备协调输入落库、助手补全与清理；
    它不保护生成过程，也不跨进程共享。revision 使清空前的排队输入失效。
    方法入口的持锁说明只指当前 Task；通用发送方法继承调用方的持锁状态。
    await 不会切换调用链所属 Task，create_task 才启动另一任务。
    Provider 的单次 I/O 超时不能作为整条调用链上限；未设总超时处最坏时长待确认。
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        session_id: str,
        device_id: str,
        chat_service: ChatService,
        identity_service: IdentityService,
        voiceprint_client: VoicePrintClient,
        vad: VoiceActivityDetector,
        recognizer: SpeechRecognizer,
        vad_pre_roll_ms: int,
        dialogue_max_turns: int,
        voiceprint_join_timeout_seconds: float,
        profile_cache: SessionProfileCache,
        profile_max_chars: int,
        memory_writer: MemoryWriter,
        dialogue_store: DialogueStore,
        dialogue_snapshot: DialogueSnapshot,
        dialogue_coordinator: DialogueCacheCoordinator,
        reminder_service: ReminderService | None = None,
        music_service: MusicService | None = None,
        podcast_service: PodcastService | None = None,
    ) -> None:
        # 运行于：websocket_endpoint Task；持锁：否；可能阻塞：同步恢复内存上下文，无异步等待。
        self.session_id = session_id
        self.device_id = device_id
        self._chat_service = chat_service
        self._identity_service = identity_service
        self._voiceprint_client = voiceprint_client
        self._vad = vad
        self._recognizer = recognizer
        self._vad_pre_roll_ms = vad_pre_roll_ms
        self._dialogue = DialogueSession(
            session_id,
            max_turns=dialogue_max_turns,
        )
        # 同一设备代表同一家庭空间。声纹只标记当前说话人，不能因为一次
        # no_match/timeout 就切断整段对话，因此按真实时间顺序恢复统一窗口。
        self._dialogue.restore(
            tuple(
                item.exchange
                for item in dialogue_snapshot.history[-dialogue_max_turns:]
            )
        )
        self._dialogue_store = dialogue_store
        self._recent_context_exclusions: deque[dict[str, str]] = deque(maxlen=dialogue_max_turns)
        if self._chat_service.context_debug.enabled:
            self._recent_context_exclusions.extend(
                {"turn_id": item.exchange.turn_id, "reason": "window_limit"}
                for item in dialogue_snapshot.history[:-dialogue_max_turns]
            )
        self._dialogue_coordinator = dialogue_coordinator
        self._dialogue_write_scope = dialogue_coordinator.write_scope(device_id)
        self._accepted_inputs: dict[str, _AcceptedUserInput] = {}
        self._pending_input_writes: set[asyncio.Task[str]] = set()
        # family 注册范围表示成员删除时需要同步移除其活跃缓存；Guest 记录
        # 没有 identity_id，不会受 remove_identity 影响。
        self._dialogue_coordinator.register(device_id, "family", self._dialogue)
        self._voiceprint_join_timeout_seconds = voiceprint_join_timeout_seconds
        self._profile_cache = profile_cache
        self._profile_max_chars = profile_max_chars
        self._memory_writer = memory_writer
        self._voice_joins: dict[str, _VoiceJoinState] = {}
        self._seen_event_ids: set[str] = set()
        self._event_id_order: deque[str] = deque()
        self._turn_transition_lock = asyncio.Lock()
        self._turn_state = _TurnState()
        self._audio_session: AudioInputSession | None = None
        self._capture_id: str | None = None
        self._audio_started_at: float | None = None
        self._closing = False
        self._pending_device_controls: dict[str, _PendingDeviceControl] = {}
        self._device_control_tasks: set[asyncio.Task[None]] = set()
        self._reminder_service = reminder_service
        self._reminder_connection_id = str(uuid4())
        self._reminder_receipts: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
        self._reminder_task: asyncio.Task[None] | None = None
        self._reminder_deliveries: dict[str, str] = {}
        self._reminder_ready = asyncio.Event()
        self._reminder_ready.set()
        self._reminder_current_turn: str | None = None
        media_resolvers = {"music": music_service} if music_service is not None else {}
        if podcast_service is not None:
            media_resolvers['podcast'] = podcast_service.session(self.device_id)
        self._media_session = MediaSession(
            device_id=self.device_id, resolvers=media_resolvers,
            emit=self.send_json,
        )
        self._sender = _OutboundSender(
            websocket,
            session_id=session_id,
            active_turn_id=lambda: self._turn_state.turn_id,
        )

    @property
    def active_turn_id(self) -> str | None:
        # 运行于：属性调用方 Task；持锁：是/否（继承调用方）；可能阻塞：无异步等待。
        return self._turn_state.turn_id

    async def start(self) -> None:
        # 运行于：websocket_endpoint Task；持锁：否；可能阻塞：无异步等待，仅启动发送和画像预取任务。
        self._sender.start()
        self._profile_cache.start()
        if self._reminder_service is not None:
            self._reminder_service.connect(self.device_id, self._reminder_connection_id, self._send_reminder_event)
            self._reminder_task = asyncio.create_task(self._record_reminder_receipts())

    def reminder_playback(self, event: dict) -> bool:
        # 接收循环只写有界队列；单一任务按顺序提交 playing/completed，避免回执乱序。
        if self._closing or self._reminder_service is None or self._reminder_receipts.full():
            return False
        if (any(not isinstance(event.get(key), str) or not 0 < len(event[key]) <= 36
                for key in ("reminder_id", "delivery_id"))
                or event.get("status") not in {"waiting", "playing", "completed", "interrupted", "failed", "expired"}):
            return False
        self._reminder_receipts.put_nowait(dict(event))
        return True

    def _release_reminder(self, delivery_id: str) -> None:
        self._reminder_deliveries.pop(delivery_id, None)
        if not self._reminder_deliveries:
            self._reminder_current_turn = None
            self._reminder_ready.set()

    async def _send_reminder_event(self, event: dict) -> None:
        if event["type"] == "reminder_due":
            item = event["reminder"]
            if not self._reminder_deliveries:
                # 到点时已开始的回复可完成；后续 Turn 在独立生成任务中等待播报。
                self._reminder_current_turn = self.active_turn_id
            self._reminder_deliveries[item["delivery_id"]] = item["reminder_id"]
            self._reminder_ready.clear()
        elif event["type"] == "reminder_delivery_ended":
            self._release_reminder(event["delivery_id"])
        elif event["type"] == "reminder_cancelled":
            for delivery_id, reminder_id in tuple(self._reminder_deliveries.items()):
                if reminder_id == event["reminder_id"]:
                    self._release_reminder(delivery_id)
        try:
            await self.send_json(event)
        except BaseException:
            if event["type"] == "reminder_due":
                self._release_reminder(event["reminder"]["delivery_id"])
            raise

    async def _record_reminder_receipts(self) -> None:
        assert self._reminder_service is not None
        while True:
            event = await self._reminder_receipts.get()
            try:
                async with asyncio.timeout(5):
                    await self._reminder_service.ack(
                        self.device_id, self._reminder_connection_id, event["reminder_id"],
                        event["delivery_id"], event["status"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reminder_receipt_failed connection_id=%s", self._reminder_connection_id)
            finally:
                self._reminder_receipts.task_done()

    async def submit_music_control(self, event: dict) -> None:
        # 两个入口只登记连接拥有的任务，接收循环不等待音源服务。
        call_id = event.get("event_id")
        if not isinstance(call_id, str) or not call_id:
            call_id = str(uuid4())
        try:
            self._media_session.submit(MediaIntent(event.get("action"), call_id, event.get("query"), event.get("index")))
        except (MediaUnavailable, ValueError) as exc:
            await self.send_json({"type": "media_error", "code": getattr(exc, "code", "invalid_request"),
                                  "message": str(exc)})

    async def request_media_control(self, command: MediaControl, *, turn_id: str | None = None) -> None:
        if self._closing or (turn_id is not None and turn_id != self.active_turn_id):
            return
        await self._media_session.publish_control(command)

    def media_result(self, event: dict) -> bool:
        return self._media_session.report(event)

    def media_state(self, event: dict) -> bool:
        return self._media_session.report_state(event)

    async def send_json(
        self,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
    ) -> None:
        # 运行于：endpoint/接收循环/识别/Turn Task；持锁：是/否（继承调用方）；可能阻塞：排队及发送回执，未设总超时。
        await self._sender.send(payload, turn_id=turn_id)

    async def send_bytes(self, payload: bytes, *, turn_id: str) -> None:
        # 运行于：Turn Task；持锁：否；可能阻塞：排队及 WebSocket 写入回执，未设本地总超时。
        await self._sender.send(payload, turn_id=turn_id)

    async def send_error(
        self,
        *,
        code: str,
        message: str,
        event_id: str | None = None,
    ) -> None:
        # 运行于：接收循环 Task；持锁：否；可能阻塞：错误事件发送回执，未设本地总超时。
        event: dict[str, Any] = {
            "type": "error",
            "code": code,
            "message": message,
        }
        if event_id:
            event["event_id"] = event_id
        await self.send_json(event)

    def remember_event(self, event_id: str) -> bool:
        # 运行于：接收循环/识别 Task；持锁：否；可能阻塞：无异步等待，仅更新去重窗口。
        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(event_id)
        self._event_id_order.append(event_id)
        if len(self._event_id_order) > _RECENT_EVENT_LIMIT:
            self._seen_event_ids.discard(self._event_id_order.popleft())
        return True

    async def start_text_turn(
        self,
        *,
        text: str,
        event_id: str,
        identity_id: str | None,
    ) -> None:
        # 运行于：接收循环 Task；持锁：否（start_turn 内获取）；可能阻塞：成员查询及 Turn 替换，未设总超时。
        action = control_from_text(text)
        if action is not None:
            await self.submit_device_control(action, event_id=event_id)
            return
        identity = None
        if identity_id is not None:
            try:
                identity = await self._identity_service.get_identity(
                    device_id=self.device_id,
                    identity_id=identity_id,
                )
            except IdentityNotFoundError:
                await self.send_error(
                    code="identity_not_found",
                    message="Selected member does not belong to this device",
                    event_id=event_id,
                )
                return
        await self.start_turn(text=text, event_id=event_id, identity=identity)

    async def submit_device_control(self, action: str, *, event_id: str) -> None:
        # 接收循环只登记任务，不内联等待 LLM/TTS 取消退出。
        if self._closing:
            return
        if action not in DEVICE_ACTIONS:
            await self.send_error(code="invalid_device_action", message="Unsupported device action",
                                  event_id=event_id)
            return
        if len(self._device_control_tasks) + len(self._pending_device_controls) >= _MAX_PENDING_DEVICE_CONTROLS:
            await self.send_error(code="device_control_busy", message="Device controls are busy",
                                  event_id=event_id)
            return
        task = asyncio.create_task(self.request_device_control(action, event_id=event_id))
        self._device_control_tasks.add(task)
        task.add_done_callback(self._device_control_finished)

    def _device_control_finished(self, task: asyncio.Task[None]) -> None:
        self._device_control_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("device_control_failed session_id=%s", self.session_id,
                         exc_info=(type(error), error, error.__traceback__))

    async def request_device_control(
        self, action: str, *, event_id: str, turn_id: str | None = None,
    ) -> None:
        # 不在识别/Turn 回调中关闭自身；页面执行后通过正常协议收尾。
        if self._closing or (turn_id is not None and turn_id != self.active_turn_id):
            return
        if action not in DEVICE_ACTIONS:
            await self.send_error(code="invalid_device_action", message="Unsupported device action",
                                  event_id=event_id)
            return
        if len(self._pending_device_controls) >= _MAX_PENDING_DEVICE_CONTROLS:
            await self.send_error(code="device_control_busy", message="Device controls are busy",
                                  event_id=event_id)
            return
        if turn_id is None and action in {"stop_reply", "disconnect"}:
            await self.cancel_turn(reason="device_control")
        request_id = str(uuid4())
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._wait_device_control(
            request_id, action, event_id, turn_id, result,
        ))
        self._pending_device_controls[request_id] = _PendingDeviceControl(action, result, task)
        try:
            await self.send_json({
                "type": "device_control", "request_id": request_id, "action": action,
                "event_id": event_id, "turn_id": turn_id,
            }, turn_id=turn_id)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._pending_device_controls.pop(request_id, None)
            raise

    def device_control_result(self, event: dict[str, Any]) -> bool:
        request_id = event.get("request_id")
        pending = self._pending_device_controls.get(request_id) if isinstance(request_id, str) else None
        status = event.get("status")
        allowed = ("accepted", "failed") if pending and pending.action == "disconnect" else (
            "completed", "not_active", "failed",
        )
        if (pending is None or pending.result.done() or event.get("action") != pending.action
                or status not in allowed):
            return False
        pending.result.set_result(status)
        return True

    async def _wait_device_control(
        self, request_id: str, action: str, event_id: str, turn_id: str | None,
        result: asyncio.Future[str],
    ) -> None:
        # 独立的有界回执任务；未回包只能认定未知，连接关闭时统一取消。
        try:
            try:
                async with asyncio.timeout(_DEVICE_CONTROL_TIMEOUT_SECONDS):
                    status = await result
            except TimeoutError:
                status = "unknown"
            logger.info("device_control_result session_id=%s request_id=%s action=%s status=%s",
                        self.session_id, request_id, action, status)
            await self.send_json({
                "type": "device_control_status", "request_id": request_id, "action": action,
                "event_id": event_id, "turn_id": turn_id, "status": status,
            })
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device_control_delivery_failed session_id=%s request_id=%s",
                             self.session_id, request_id)
        finally:
            self._pending_device_controls.pop(request_id, None)

    async def clear_dialogue(
        self,
        *,
        event_id: str,
    ) -> None:
        # 运行于：接收循环 Task；持锁：否（cancel_turn 内获取）；可能阻塞：旧任务清理、数据库清空及发送，未设总超时。
        await self.cancel_turn(reason="dialogue_cleared")
        try:
            async with self._dialogue_write_scope.lock:
                deleted = await self._dialogue_store.clear(session_id=self.session_id)
                self._dialogue_coordinator.clear(self.device_id)
        except Exception:
            logger.exception(
                "dialogue_clear_failed session_id=%s scope=all",
                self.session_id,
            )
            await self.send_error(
                code="dialogue_clear_failed",
                message="Unable to clear the current dialogue",
                event_id=event_id,
            )
            return

        logger.info(
            "dialogue_cleared session_id=%s device_id=%s scope=all deleted=%s",
            self.session_id,
            self.device_id,
            deleted,
        )
        await self.send_json(
            {
                "type": "dialogue_cleared",
                "event_id": event_id,
                "session_id": self.session_id,
                "scope": "all",
                "deleted_turns": deleted,
            }
        )

    async def start_turn(
        self,
        *,
        text: str,
        event_id: str,
        identity: Identity | None,
        speaker_resolution: _SpeakerResolution | None = None,
    ) -> None:
        # 运行于：接收循环/识别 Task；持锁：否（随后获取并持有切换锁）；可能阻塞：等锁、旧任务清理及发送，未设总超时。
        # 文本输入和 ASR Final 可能同时到达。整个替换过程必须串行，
        # 否则新 Turn 可能在旧 Turn 保存 task 引用前完成取消，留下孤儿任务。
        async with self._turn_transition_lock:
            if self._closing:
                return
            if len(self._pending_input_writes) >= _MAX_PENDING_INPUT_WRITES:
                await self.send_error(
                    code="dialogue_busy",
                    message="Dialogue storage is busy; retry this input",
                    event_id=event_id,
                )
                return
            await self._cancel_turn(reason="superseded")
            context_started_at = perf_counter()
            dialogue = self._dialogue
            speaker_display_name = (
                identity.display_name if identity is not None else "Guest"
            )
            speaker_relationship = (
                identity.relationship if identity is not None else None
            )
            speaker_identity_id = (
                identity.identity_id if identity is not None else None
            )
            profile_snapshot = (
                self._profile_cache.snapshot_for(speaker_identity_id)
                if speaker_identity_id is not None
                else None
            )
            profile_prompt = (
                profile_snapshot.to_prompt(max_chars=self._profile_max_chars)
                if profile_snapshot is not None
                else None
            )
            context_messages = (
                (ChatMessage(role="system", content=profile_prompt),)
                if profile_prompt
                else ()
            )
            if self._media_session.enabled or self._media_session.observation.action != 'none':
                context_messages = (*context_messages, ChatMessage(
                    role="user", content=self._media_session.prompt()))
            messages = dialogue.messages_for(
                text,
                speaker_identity_id=speaker_identity_id,
                speaker_display_name=speaker_display_name,
                speaker_relationship=speaker_relationship,
                context_messages=context_messages,
            )
            turn = self._chat_service.create_turn(
                session_id=self.session_id,
                user_text=text,
                messages=messages,
                device_id=self.device_id,
                speaker_identity_id=speaker_identity_id,
                speaker_display_name=speaker_display_name,
                speaker_relationship=speaker_relationship,
                profile_ready=profile_snapshot is not None,
                profile_field_count=(
                    len(profile_snapshot.fields) if profile_snapshot is not None else 0
                ),
            )
            if self._chat_service.context_debug.enabled:
                # 此处仅记录候选；系统提示和工具加入后的最终取舍由模型适配器记录。
                self._record_context(
                    turn.turn_id, "context_selected", event_id=event_id,
                    source="voice" if event_id.startswith("voice-") else "text",
                    identity={
                        "identity_id": speaker_identity_id,
                        "display_name": speaker_display_name,
                        "reason": speaker_resolution.reason if speaker_resolution else (
                            "explicit_member" if identity is not None else "explicit_guest"
                        ),
                        "score": speaker_resolution.score if speaker_resolution else None,
                    },
                    profile={"enabled": self._profile_cache.enabled,
                             "ready": turn.profile_ready, "fields": turn.profile_field_count,
                             "injected": bool(profile_prompt), "chars": len(profile_prompt or "")},
                    limits={"max_turns": dialogue.max_turns},
                    history=[
                        {"turn_id": exchange.turn_id, "reply_status": (
                            "completed" if exchange.assistant_text else "user_only"
                        ), "reason": "candidate"}
                        for exchange in dialogue.exchanges
                    ],
                    recent_exclusions=list(self._recent_context_exclusions),
                    messages=[{"role": message.role, "content": message.content} for message in messages],
                )
            if speaker_resolution is not None:
                self._record_context(
                    turn.turn_id, "voice_input_timing", utterance_id=event_id.removeprefix("voice-"),
                    **dict(speaker_resolution.timing),
                )
            evicted_turn_id = (
                dialogue.exchanges[0].turn_id
                if len(dialogue.exchanges) == dialogue.max_turns else None
            )
            self._dialogue_coordinator.accept_user_input(self.device_id, turn)
            if evicted_turn_id is not None:
                self._exclude_from_context(evicted_turn_id, "window_limit")
            revision = self._dialogue_write_scope.revision
            persistence = asyncio.create_task(self._persist_user_input(turn, revision))
            self._pending_input_writes.add(persistence)
            persistence.add_done_callback(self._pending_input_writes.discard)
            self._accepted_inputs[turn.turn_id] = _AcceptedUserInput(revision, persistence)
            self._record_context(turn.turn_id, "user_input_accepted", dialogue_memory="user_only")
            self._turn_state.activate(turn.turn_id)
            logger.info(
                "turn_started session_id=%s turn_id=%s event_id=%s",
                self.session_id,
                turn.turn_id,
                event_id,
            )
            logger.info(
                "context_candidates_ready session_id=%s turn_id=%s window_inputs=%s messages=%s chars=%s elapsed_ms=%.1f",
                self.session_id,
                turn.turn_id,
                len(dialogue.exchanges),
                len(messages),
                sum(len(message.content) for message in messages),
                (perf_counter() - context_started_at) * 1000,
            )
            try:
                await self.send_json(
                    {
                        "type": "turn_started",
                        "session_id": self.session_id,
                        "turn_id": turn.turn_id,
                        "event_id": event_id,
                        "speaker": {
                            "identity_id": speaker_identity_id,
                            "display_name": speaker_display_name,
                            "guest": identity is None,
                        },
                        "profile": {
                            "enabled": self._profile_cache.enabled,
                            "ready": turn.profile_ready,
                            "fields": turn.profile_field_count,
                        },
                    },
                    turn_id=turn.turn_id,
                )
            except BaseException:
                self._turn_state.clear_active(turn.turn_id)
                self._accepted_inputs.pop(turn.turn_id, None)
                raise
            task = asyncio.create_task(
                self._stream_turn(turn, event_id=event_id, dialogue=dialogue)
            )
            self._turn_state.attach_task(turn.turn_id, task)

    async def cancel_turn(self, *, reason: str, notify: bool = True) -> None:
        # 运行于：接收循环或 endpoint 清理 Task；持锁：否（随后获取切换锁）；可能阻塞：等锁、旧任务清理及通知，未设总超时。
        async with self._turn_transition_lock:
            await self._cancel_turn(reason=reason, notify=notify)

    async def _cancel_turn(self, *, reason: str, notify: bool = True) -> None:
        # 运行于：接收循环/识别/endpoint 清理 Task；持锁：是；可能阻塞：旧 Turn 取消清理和发送通知，未设总超时。
        cancellation = self._turn_state.take_for_cancellation()
        if cancellation.turn_id is None and cancellation.playback_turn_id is None:
            return

        if cancellation.task is not None:
            cancellation.task.cancel()
            await asyncio.gather(cancellation.task, return_exceptions=True)
        if cancellation.turn_id is not None:
            self._accepted_inputs.pop(cancellation.turn_id, None)
            self._record_context(cancellation.turn_id, "turn_cancelled", reason=reason)
            logger.info(
                "turn_cancelled session_id=%s turn_id=%s reason=%s",
                self.session_id,
                cancellation.turn_id,
                reason,
            )
        elif cancellation.playback_turn_id is not None:
            logger.info(
                "playback_interrupted session_id=%s turn_id=%s stream_id=%s reason=%s",
                self.session_id,
                cancellation.playback_turn_id,
                cancellation.playback_stream_id,
                reason,
            )
        if notify and not self._closing:
            if cancellation.turn_id is not None:
                await self.send_json(
                    {
                        "type": "turn_cancelled",
                        "turn_id": cancellation.turn_id,
                        "reason": reason,
                    }
                )
            audio_turn_id = cancellation.playback_turn_id or cancellation.turn_id
            if audio_turn_id is not None:
                await self.send_json(
                    {
                        "type": "audio_stop",
                        "turn_id": audio_turn_id,
                        "stream_id": (
                            cancellation.playback_stream_id
                            or cancellation.stream_id
                        ),
                        "reason": reason,
                    }
                )

    async def playback_drained(self, *, turn_id: str, stream_id: str) -> None:
        # 运行于：接收循环 Task；持锁：否；可能阻塞：无异步等待，仅核对并清除播放状态。
        if not self._turn_state.clear_playback(turn_id, stream_id):
            return
        logger.info(
            "browser_playback_drained session_id=%s turn_id=%s stream_id=%s",
            self.session_id,
            turn_id,
            stream_id,
        )

    async def start_audio_input(self, event: dict[str, Any]) -> None:
        # 运行于：接收循环 Task；持锁：否；可能阻塞：同步创建 VAD 流及发送回执，未设本地总超时。
        event_id = _string_or_none(event.get("event_id"))
        capture_id = _string_or_none(event.get("capture_id"))
        if not capture_id:
            await self.send_error(
                code="invalid_capture_id",
                message="audio_input_start requires capture_id",
                event_id=event_id,
            )
            return
        if self._audio_session is not None:
            await self.send_error(
                code="audio_input_active",
                message="Only one audio input can be active per connection",
                event_id=event_id,
            )
            return
        if not _matches_input_format(event.get("format")):
            await self.send_error(
                code="unsupported_audio_format",
                message="Audio input must be 16kHz mono PCM S16LE with 20ms frames",
                event_id=event_id,
            )
            return

        self._capture_id = capture_id
        self._audio_started_at = perf_counter()
        self._audio_session = AudioInputSession(
            vad_stream=self._vad.create_stream(),
            recognizer=self._recognizer,
            on_boundary=self._on_speech_boundary,
            on_asr_event=self._on_asr_event,
            on_asr_error=self._on_asr_error,
            on_utterance_audio=self._on_utterance_audio,
            pre_roll_ms=self._vad_pre_roll_ms,
        )
        await self.send_json(
            {
                "type": "audio_input_ready",
                "capture_id": capture_id,
                "event_id": event_id,
            }
        )
        logger.info(
            "audio_input_started session_id=%s capture_id=%s",
            self.session_id,
            capture_id,
        )

    async def push_audio(self, pcm: bytes) -> None:
        # 运行于：接收循环 Task；持锁：否（边界回调可获取）；可能阻塞：VAD 推理、边界/错误通知及取消清理，未设总超时。
        if self._audio_session is None:
            await self.send_error(
                code="audio_input_not_started",
                message="Send audio_input_start before binary audio frames",
            )
            return
        try:
            await self._audio_session.push(pcm)
        except ValueError as exc:
            await self.send_error(code="invalid_audio_frame", message=str(exc))

    async def stop_audio_input(self, event: dict[str, Any]) -> None:
        # 运行于：接收循环 Task；持锁：否；可能阻塞：等待 ASR Final、识别回调及发送，未设总超时，不能仅按 ASR 单次超时估算。
        event_id = _string_or_none(event.get("event_id"))
        capture_id = _string_or_none(event.get("capture_id"))
        if self._audio_session is None or capture_id != self._capture_id:
            await self.send_error(
                code="audio_input_not_found",
                message="capture_id does not match the active audio input",
                event_id=event_id,
            )
            return
        session = self._audio_session
        self._audio_session = None
        self._capture_id = None
        await session.close()
        await self.send_json(
            {
                "type": "audio_input_stopped",
                "capture_id": capture_id,
                "event_id": event_id,
            }
        )
        logger.info(
            "audio_input_stopped session_id=%s capture_id=%s",
            self.session_id,
            capture_id,
        )

    async def close(self) -> None:
        # 运行于：endpoint 的 finally；持锁：否（cancel_turn 内获取）；可能阻塞：画像/ASR/声纹/Turn/输入落库/发送任务收尾，未设总超时。
        if self._closing:
            return
        self._closing = True
        await self._media_session.close()
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            await asyncio.gather(self._reminder_task, return_exceptions=True)
        if self._reminder_service is not None:
            try:
                async with asyncio.timeout(5):
                    await self._reminder_service.disconnect(self.device_id, self._reminder_connection_id)
            except Exception:
                logger.exception("reminder_disconnect_failed connection_id=%s", self._reminder_connection_id)
        commands = tuple(self._device_control_tasks)
        for task in commands:
            task.cancel()
        if commands:
            await asyncio.gather(*commands, return_exceptions=True)
        self._device_control_tasks.clear()
        control_tasks = [pending.task for pending in self._pending_device_controls.values()]
        for task in control_tasks:
            task.cancel()
        if control_tasks:
            await asyncio.gather(*control_tasks, return_exceptions=True)
        self._pending_device_controls.clear()
        await self._profile_cache.close()
        if self._audio_session is not None:
            session = self._audio_session
            self._audio_session = None
            with suppress(Exception):
                await session.abort()
        voiceprint_tasks = [
            state.task
            for state in self._voice_joins.values()
            if state.task is not None and not state.task.done()
        ]
        for task in voiceprint_tasks:
            task.cancel()
        if voiceprint_tasks:
            await asyncio.gather(*voiceprint_tasks, return_exceptions=True)
        self._voice_joins.clear()
        await self.cancel_turn(reason="connection_closed", notify=False)
        # 正常断开不能取消已接受输入的落库；只取消生成，独立输入任务需收尾。
        if self._pending_input_writes:
            await asyncio.gather(*tuple(self._pending_input_writes), return_exceptions=True)
        self._dialogue_coordinator.unregister(self._dialogue)
        await self._sender.close()

    async def _persist_user_input(self, turn: Turn, revision: int) -> str:
        """独立输入 Task；不在接收循环等待 DB，不随 LLM/TTS 取消。"""
        status = "failed"
        try:
            async with self._dialogue_write_scope.lock:
                if not self._dialogue_write_scope.permits(revision, turn.speaker_identity_id):
                    status = "skipped"
                elif not await self._can_commit_turn(turn):
                    status = "skipped"
                else:
                    exchange = DialogueExchange(
                        turn_id=turn.turn_id,
                        user_text=turn.user_text,
                        assistant_text="",
                        speaker_identity_id=turn.speaker_identity_id,
                        speaker_display_name=turn.speaker_display_name,
                        speaker_relationship=turn.speaker_relationship,
                    )
                    inserted = await self._dialogue_store.append(
                        session_id=self.session_id,
                        lane="family" if turn.speaker_identity_id is not None else "guest",
                        exchange=exchange,
                        accepted_at=turn.created_at,
                    )
                    status = "completed" if inserted else "skipped"
        except asyncio.CancelledError:
            self._record_context(turn.turn_id, "user_input_persistence", status="unknown")
            raise
        except Exception:
            logger.exception(
                "user_input_persist_failed session_id=%s turn_id=%s",
                self.session_id,
                turn.turn_id,
            )
        self._record_context(turn.turn_id, "user_input_persistence", status=status)
        return status

    def _record_context(self, turn_id: str, event: str, **fields: Any) -> None:
        # 运行于：调用方 Task；持锁：继承调用方；可能阻塞：仅显式开启时写本机调试文件。
        self._chat_service.context_debug.record(
            event, turn_id=turn_id, session_id=self.session_id, device_id=self.device_id,
            **fields,
        )

    def record_playback_timing(self, event: dict) -> None:
        # 运行于：接收循环 Task；持锁：否；可能阻塞：仅本机日志，不等待 Provider。
        # 这是浏览器自报的设备输出估计，不用于身份、Turn 状态或计费判断。
        self._record_context(
            event["turn_id"], "playback_audible", stream_id=event["stream_id"],
            output_clock=event["output_clock"], metrics=event["metrics"],
        )
        logger.info(
            "browser_playback_audible session_id=%s turn_id=%s stream_id=%s "
            "output_clock=%s metrics=%s",
            self.session_id, event["turn_id"], event["stream_id"],
            event["output_clock"], event["metrics"],
        )

    def _exclude_from_context(self, turn_id: str, reason: str) -> None:
        # 仅保留本连接近期观察到的排除原因，不冒充完整数据库历史或改变 Dialogue。
        if not self._chat_service.context_debug.enabled:
            return
        if any(item["turn_id"] == turn_id for item in self._recent_context_exclusions):
            return
        self._recent_context_exclusions.append({"turn_id": turn_id, "reason": reason})
        self._record_context(
            turn_id, "context_excluded", excluded_turn_id=turn_id, reason=reason,
        )

    async def _stream_turn(
        self,
        turn,
        *,
        event_id: str,
        dialogue: DialogueSession,
    ) -> None:
        # 运行于：Turn Task；持锁：否；可能阻塞：LLM/TTS、成员校验、Dialogue 持久化、记忆入队及发送，未设总超时。
        started_at = perf_counter()
        first_text_ms: float | None = None
        first_audio_ms: float | None = None
        audio_complete_ms: float | None = None
        text_chunks = 0
        audio_frames = 0
        audio_bytes = 0
        summary_logged = False
        accepted = self._accepted_inputs[turn.turn_id]
        outcome = "started"
        dialogue_memory = "user_only"
        dialogue_database = "not_attempted"
        memory_queue = "not_attempted"
        response_text: str | None = None

        def log_summary(status: str, *, response_chars: int = 0) -> None:
            nonlocal summary_logged
            if summary_logged:
                return
            summary_logged = True
            self._record_context(
                turn.turn_id, "turn_latency", status=status,
                first_text_ms=first_text_ms, first_audio_ms=first_audio_ms,
                audio_complete_ms=audio_complete_ms,
                total_ms=(perf_counter() - started_at) * 1000,
            )
            logger.info(
                "turn_latency_summary session_id=%s turn_id=%s source=%s "
                "speaker=%s status=%s context_messages=%s prompt_chars=%s "
                "input_chars=%s response_chars=%s text_chunks=%s "
                "audio_frames=%s audio_bytes=%s first_text_ms=%s "
                "first_audio_ms=%s audio_complete_ms=%s total_ms=%.1f",
                self.session_id,
                turn.turn_id,
                "voice" if event_id.startswith("voice-") else "text",
                "member" if turn.speaker_identity_id is not None else "guest",
                status,
                max(0, len(turn.messages) - 1),
                sum(len(message.content) for message in turn.messages),
                len(turn.user_text),
                response_chars,
                text_chunks,
                audio_frames,
                audio_bytes,
                _format_optional_ms(first_text_ms),
                _format_optional_ms(first_audio_ms),
                _format_optional_ms(audio_complete_ms),
                (perf_counter() - started_at) * 1000,
            )

        try:
            if self._reminder_deliveries and turn.turn_id != self._reminder_current_turn:
                await self.send_json({
                    "type": "turn_waiting", "turn_id": turn.turn_id, "reason": "reminder_playback",
                }, turn_id=turn.turn_id)
                # 最后一条结束的同一调度间隙可能又有提醒到点；醒来后重新检查。
                while self._reminder_deliveries:
                    await self._reminder_ready.wait()
                if self._closing or self.active_turn_id != turn.turn_id:
                    return
            stream = (
                self._chat_service.stream_turn(turn, device_control_enabled=True, media_commands=self._media_session.for_turn(
                    lambda: not self._closing and self.active_turn_id == turn.turn_id))
                if self._media_session.enabled else self._chat_service.stream_turn(turn, device_control_enabled=True)
            )
            async with aclosing(stream) as outputs:
                async for output in outputs:
                    if isinstance(output, MediaControl):
                        await self.request_media_control(output, turn_id=turn.turn_id)
                        await self.send_json({"type": "turn_cancelled", "turn_id": turn.turn_id,
                                              "reason": "media_control"}, turn_id=turn.turn_id)
                        outcome = "controlled"
                        log_summary("controlled")
                        return
                    if isinstance(output, DeviceControl):
                        await self.request_device_control(output.action, event_id=event_id,
                                                          turn_id=turn.turn_id)
                        await self.send_json({
                            "type": "turn_cancelled", "turn_id": turn.turn_id,
                            "reason": "device_control",
                        }, turn_id=turn.turn_id)
                        outcome = "controlled"
                        log_summary("controlled")
                        return
                    if isinstance(output, TextDelta):
                        text_chunks += 1
                        if first_text_ms is None:
                            first_text_ms = (perf_counter() - started_at) * 1000
                        await self.send_json(
                            {
                                "type": "text_delta",
                                "turn_id": turn.turn_id,
                                "event_id": event_id,
                                "sequence": output.sequence,
                                "delta": output.text,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, AudioStarted):
                        if first_audio_ms is None:
                            first_audio_ms = (perf_counter() - started_at) * 1000
                        self._turn_state.begin_playback(
                            turn.turn_id,
                            output.stream_id,
                        )
                        await self.send_json(
                            {
                                "type": "audio_start",
                                "turn_id": turn.turn_id,
                                "stream_id": output.stream_id,
                                "codec": output.audio_format.codec,
                                "sample_rate": output.audio_format.sample_rate,
                                "channels": output.audio_format.channels,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, AudioFrame):
                        await self.send_bytes(output.data, turn_id=turn.turn_id)
                    elif isinstance(output, AudioCompleted):
                        audio_frames = output.frame_count
                        audio_bytes = output.byte_count
                        audio_complete_ms = (perf_counter() - started_at) * 1000
                        await self.send_json(
                            {
                                "type": "audio_end",
                                "turn_id": turn.turn_id,
                                "stream_id": output.stream_id,
                                "frames": output.frame_count,
                                "bytes": output.byte_count,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, AudioFailed):
                        self._turn_state.clear_playback(
                            turn.turn_id,
                            output.stream_id,
                        )
                        await self.send_json(
                            {
                                "type": "audio_failed",
                                "turn_id": turn.turn_id,
                                "stream_id": output.stream_id,
                                "message": output.message,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, TurnCompleted):
                        response_text = output.text
                        log_summary("completed", response_chars=len(output.text))
                        committed = False
                        persisted = False
                        if (
                            self._turn_state.turn_id == turn.turn_id
                            and self._dialogue_write_scope.permits(
                                accepted.revision, turn.speaker_identity_id
                            )
                            and await self._can_commit_turn(turn)
                        ):
                            # 补全原 user-only 位置，不追加第二条输入，也不重新插入已淘汰记录。
                            committed = self._dialogue_coordinator.commit(
                                self.device_id,
                                turn,
                                output.text,
                            )
                            dialogue_memory = "completed" if committed else "not_retained"
                            try:
                                # await 被取消时无法确定数据库是否已提交，不能谎报“未保存”。
                                dialogue_database = "unknown"
                                self._record_context(
                                    turn.turn_id, "turn_commit", stage="dialogue_database", status="started",
                                )
                                # shield 防止回复取消沿 await 传播到独立输入落库 Task。
                                input_status = await asyncio.shield(accepted.persistence)
                                if committed and input_status == "completed":
                                    async with self._dialogue_write_scope.lock:
                                        if self._dialogue_write_scope.permits(
                                            accepted.revision, turn.speaker_identity_id
                                        ):
                                            exchange = next(
                                                (item for item in dialogue.exchanges
                                                 if item.turn_id == turn.turn_id),
                                                None,
                                            )
                                            if exchange is not None:
                                                persisted = await self._dialogue_store.complete(
                                                    session_id=self.session_id, exchange=exchange,
                                                )
                                dialogue_database = "completed" if persisted else (
                                    "failed" if input_status == "failed" else "not_retained"
                                )
                            except Exception:
                                dialogue_database = "failed"
                                logger.exception(
                                    "dialogue_persist_failed session_id=%s turn_id=%s",
                                    self.session_id,
                                    turn.turn_id,
                                )
                            logger.info(
                                "dialogue_committed session_id=%s turn_id=%s "
                                "window_inputs=%s persisted=%s",
                                self.session_id,
                                turn.turn_id,
                                len(dialogue.exchanges),
                                persisted,
                            )
                        if (
                            committed
                            and persisted
                            and turn.speaker_identity_id is not None
                        ):
                            try:
                                memory_queue = "unknown"
                                self._record_context(
                                    turn.turn_id, "turn_commit", stage="memory_queue", status="started",
                                )
                                inserted = await self._memory_writer.enqueue_completed_turn(
                                    turn, output.text
                                )
                                memory_queue = "inserted" if inserted else "not_inserted"
                            except Exception:
                                memory_queue = "failed"
                                logger.exception(
                                    "memory_job_enqueue_failed session_id=%s turn_id=%s",
                                    self.session_id,
                                    turn.turn_id,
                                )
                        await self.send_json(
                            {
                                "type": "turn_completed",
                                "turn_id": turn.turn_id,
                                "event_id": event_id,
                                "text": output.text,
                            },
                            turn_id=turn.turn_id,
                        )
                        outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            log_summary("cancelled")
            raise
        except Exception as exc:
            outcome = "failed"
            self._record_context(turn.turn_id, "assistant_reply_failed")
            log_summary("failed")
            if isinstance(exc, ContextBudgetExceeded):
                logger.warning(
                    "context_budget_exceeded session_id=%s turn_id=%s limit=%s estimated=%s",
                    self.session_id, turn.turn_id,
                    exc.decision["input_token_limit"], exc.decision["after_tokens_estimate"],
                )
            else:
                logger.exception(
                    "turn_failed session_id=%s turn_id=%s event_id=%s",
                    self.session_id,
                    turn.turn_id,
                    event_id,
                )
            await self.send_json(
                {
                    "type": "turn_failed",
                    "turn_id": turn.turn_id,
                    "event_id": event_id,
                    "code": "context_budget_exceeded" if isinstance(exc, ContextBudgetExceeded) else "chat_failed",
                    "message": (
                        "System context and current input exceed the input budget; shorten the input or adjust the budget"
                        if isinstance(exc, ContextBudgetExceeded) else "Unable to generate a reply"
                    ),
                },
                turn_id=turn.turn_id,
            )
        finally:
            user_input_database = (
                "unknown" if accepted.persistence.cancelled() else
                accepted.persistence.result() if accepted.persistence.done() else "pending"
            )
            self._record_context(
                turn.turn_id, "turn_end", outcome=outcome,
                dialogue_memory=dialogue_memory, dialogue_database=dialogue_database,
                memory_queue=memory_queue, memory_writer=type(self._memory_writer).__name__,
                memory_skip_reason=(
                    "guest" if turn.speaker_identity_id is None else
                    "turn_not_completed" if outcome != "completed" else "dialogue_not_persisted"
                ) if memory_queue == "not_attempted" else None,
                response_text=response_text,
                user_input_memory=("retained" if any(
                    item.turn_id == turn.turn_id for item in dialogue.exchanges
                ) else "not_retained"),
                user_input_database=user_input_database,
            )
            self._accepted_inputs.pop(turn.turn_id, None)
            if self._turn_state.clear_active(turn.turn_id):
                logger.info(
                    "turn_completed session_id=%s turn_id=%s",
                    self.session_id,
                    turn.turn_id,
                )

    async def _can_commit_turn(self, turn: Turn) -> bool:
        # 运行于：Turn/独立输入落库 Task；持锁：输入任务持设备写锁，Turn 不持；可能阻塞：成员有效性数据库查询，未设本地总超时。
        if turn.speaker_identity_id is None:
            return True
        try:
            await self._identity_service.get_identity(
                device_id=turn.device_id,
                identity_id=turn.speaker_identity_id,
            )
            return True
        except IdentityNotFoundError:
            logger.info(
                "dialogue_commit_skipped_inactive_identity "
                "session_id=%s turn_id=%s identity_id=%s",
                self.session_id,
                turn.turn_id,
                turn.speaker_identity_id,
            )
            return False
        except Exception:
            logger.exception(
                "dialogue_commit_identity_check_failed "
                "session_id=%s turn_id=%s identity_id=%s",
                self.session_id,
                turn.turn_id,
                turn.speaker_identity_id,
            )
            # 回复已经可以继续交付当前连接，但无法确认成员仍有效时，不能把
            # 私人 Dialogue 或 Memory 写入长期存储。
            return False

    async def _on_speech_boundary(self, event: SpeechBoundary) -> None:
        # 运行于：接收循环 Task（push/stop 回调）；持锁：否（cancel_turn 内获取）；可能阻塞：边界发送和旧 Turn 取消清理，未设总超时。
        if self._closing:
            return
        # 以开口时的模式固定该语音段；播放结束后的迟到识别不能变成新聊天。
        media = self._media_session.observation
        media_only = media.voice_media_only
        if event.kind == 'speech_start':
            self._voice_joins[event.utterance_id] = _VoiceJoinState(
                media_only=media_only, media_request=(media.request_id, media.operation_id) if media_only else None)
        else:
            state = self._voice_joins.get(event.utterance_id)
            if state is not None:
                media_only = state.media_only
        if event.kind == "speech_end":
            state = self._voice_joins.get(event.utterance_id)
            if state is not None:
                state.speech_ended_at = perf_counter()
                if event.last_voice_audio_ms is not None:
                    state.vad_silence_ms = event.audio_ms - event.last_voice_audio_ms
        event_type = "vad_speech_start" if event.kind == "speech_start" else "vad_speech_end"
        elapsed_ms = (
            (perf_counter() - self._audio_started_at) * 1000
            if self._audio_started_at is not None
            else 0.0
        )
        await self.send_json(
            {
                "type": event_type,
                "capture_id": self._capture_id,
                "utterance_id": event.utterance_id,
                "probability": round(event.probability, 4),
                "audio_ms": round(event.audio_ms, 1),
                "last_voice_audio_ms": event.last_voice_audio_ms,
                "media_only": media_only,
            }
        )
        logger.info(
            "%s session_id=%s utterance_id=%s audio_ms=%.1f elapsed_ms=%.1f",
            event_type,
            self.session_id,
            event.utterance_id,
            event.audio_ms,
            elapsed_ms,
        )
        if event.kind == "speech_start" and not media_only:
            await self.cancel_turn(reason="barge_in")

    async def _on_utterance_audio(self, utterance_id: str, pcm: bytes) -> None:
        # 运行于：接收循环 Task（push/stop 回调）；持锁：否；可能阻塞：同步 PCM 转 WAV，无异步等待，不等待声纹请求完成。
        if self._closing:
            return
        state = self._voice_joins.setdefault(utterance_id, _VoiceJoinState())
        if state.media_only:
            state.ready.set()
            return
        try:
            wav = pcm_s16le_to_wav(pcm)
        except ValueError:
            state.failure_reason = "invalid_audio"
            state.ready.set()
            return
        state.task = asyncio.create_task(
            self._voiceprint_client.identify(device_id=self.device_id, sample=wav)
        )
        state.ready.set()

    async def _on_asr_event(self, utterance_id: str, event) -> None:
        # 运行于：识别 Task；持锁：否（start_turn 内获取）；可能阻塞：发送、声纹汇合、成员查询及 Turn 替换，未设总超时。
        if self._closing:
            return
        if isinstance(event, AsrPartial):
            state = self._voice_joins.get(utterance_id)
            if (state.media_only if state is not None else self._media_session.observation.voice_media_only):
                return
            await self.send_json(
                {
                    "type": "asr_partial",
                    "utterance_id": utterance_id,
                    "text": event.text,
                }
            )
            return
        if isinstance(event, AsrFinal):
            state = self._voice_joins.get(utterance_id)
            if state is not None:
                state.asr_final_at = perf_counter()
            text = event.text.strip()
            media_only = state.media_only if state is not None else self._media_session.observation.voice_media_only
            media_action = playback_stop_from_text(text) if media_only else None
            media = self._media_session.observation
            media_request = state.media_request if state is not None else (media.request_id, media.operation_id)
            if not media.voice_media_only or media_request != (media.request_id, media.operation_id):
                media_action = None
            disposition = ('media_control' if media_action else 'ignored_media') if media_only else 'chat'
            await self.send_json(
                {
                    "type": "asr_final",
                    "utterance_id": utterance_id,
                    "text": '' if disposition == 'ignored_media' else text,
                    "disposition": disposition,
                }
            )
            logger.info(
                "asr_final session_id=%s utterance_id=%s recognizer=%s text_length=%s",
                self.session_id,
                utterance_id,
                type(self._recognizer).__name__,
                len(text),
            )
            if media_only:
                await self._discard_voice_join(utterance_id)
                if media_action is not None:
                    event_id = f'voice-{utterance_id}'
                    if not self.remember_event(event_id):
                        return
                    media = self._media_session.observation
                    if not media.voice_media_only or media_request != (media.request_id, media.operation_id):
                        return
                    try:
                        self._media_session.submit(MediaIntent(media_action, event_id, source=self._media_session.active_source))
                    except (MediaUnavailable, ValueError) as exc:
                        await self.send_json({'type': 'media_error', 'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc)})
                        return
                    logger.info('media_voice_control session_id=%s utterance_id=%s action=%s',
                                self.session_id, utterance_id, media_action)
                    await self.cancel_turn(reason='media_voice_control')
                else:
                    logger.info('voice_input_ignored session_id=%s utterance_id=%s reason=media_only text_length=%s',
                                self.session_id, utterance_id, len(text))
                return
            if text:
                event_id = f"voice-{utterance_id}"
                if not self.remember_event(event_id):
                    return
                action = control_from_text(text)
                if action is not None:
                    await self._discard_voice_join(utterance_id)
                    await self.submit_device_control(action, event_id=event_id)
                    return
                resolution = await self._resolve_voice_speaker(utterance_id)
                await self.send_json(
                    {
                        "type": "speaker_resolved",
                        "utterance_id": utterance_id,
                        "identity_id": (
                            resolution.identity.identity_id
                            if resolution.identity is not None
                            else None
                        ),
                        "display_name": (
                            resolution.identity.display_name
                            if resolution.identity is not None
                            else "Guest"
                        ),
                        "matched": resolution.identity is not None,
                        "score": round(resolution.score, 4),
                        "reason": resolution.reason,
                        "provider_elapsed_ms": (
                            round(resolution.provider_elapsed_ms, 1)
                            if resolution.provider_elapsed_ms is not None
                            else None
                        ),
                        "elapsed_ms": round(resolution.elapsed_ms, 1),
                    }
                )
                await self.start_turn(
                    text=text,
                    event_id=event_id,
                    identity=resolution.identity,
                    speaker_resolution=resolution,
                )
            else:
                await self._discard_voice_join(utterance_id)

    async def _resolve_voice_speaker(self, utterance_id: str) -> _SpeakerResolution:
        # 运行于：识别 Task；持锁：否；可能阻塞：声纹汇合预算为 _voiceprint_join_timeout_seconds，取消清理及后续成员查询无总超时。
        state = self._voice_joins.setdefault(utterance_id, _VoiceJoinState())
        join_started_at = perf_counter()
        deadline = asyncio.get_running_loop().time() + self._voiceprint_join_timeout_seconds
        identification: VoicePrintIdentification | None = None
        reason = state.failure_reason or "unavailable"
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(state.ready.wait(), timeout=remaining)
            if state.task is not None:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                identification = await asyncio.wait_for(state.task, timeout=remaining)
                reason = "matched" if identification.matched else "no_match"
        except TimeoutError:
            reason = "timeout"
            if state.task is not None:
                state.task.cancel()
        except VoicePrintUnavailableError:
            reason = "unavailable"
        except VoicePrintError:
            reason = "invalid_audio"
        except Exception:
            reason = "failed"
            logger.exception(
                "voiceprint_failed session_id=%s utterance_id=%s",
                self.session_id,
                utterance_id,
            )

        identity = None
        if identification is not None and identification.matched and identification.identity_id:
            try:
                identity = await self._identity_service.get_identity(
                    device_id=self.device_id,
                    identity_id=identification.identity_id,
                )
            except IdentityNotFoundError:
                reason = "identity_missing"
        elapsed_ms = (perf_counter() - state.started_at) * 1000
        join_wait_ms = (perf_counter() - join_started_at) * 1000
        # ASR 与声纹并行：记录共同判停基准和汇合等待，不能把两个总耗时相加。
        timing = (
            ("vad_silence_ms", state.vad_silence_ms),
            ("vad_to_asr_final_ms", (
                (state.asr_final_at - state.speech_ended_at) * 1000
                if state.asr_final_at is not None and state.speech_ended_at is not None else None
            )),
            ("vad_to_speaker_ready_ms", (
                (perf_counter() - state.speech_ended_at) * 1000
                if state.speech_ended_at is not None else None
            )),
            ("speaker_join_ms", join_wait_ms),
            ("voiceprint_provider_ms", identification.elapsed_ms if identification else None),
        )
        await self._discard_voice_join(utterance_id, cancel=False)
        logger.info(
            "speaker_resolved session_id=%s utterance_id=%s identity_id=%s "
            "reason=%s score=%.4f provider_elapsed_ms=%s join_wait_ms=%.1f "
            "join_budget_ms=%.1f total_elapsed_ms=%.1f",
            self.session_id,
            utterance_id,
            identity.identity_id if identity is not None else "guest",
            reason,
            identification.score if identification is not None else 0.0,
            (
                f"{identification.elapsed_ms:.1f}"
                if identification is not None
                else "none"
            ),
            join_wait_ms,
            self._voiceprint_join_timeout_seconds * 1000,
            elapsed_ms,
        )
        return _SpeakerResolution(
            identity=identity,
            score=identification.score if identification is not None else 0.0,
            reason=reason,
            provider_elapsed_ms=(
                identification.elapsed_ms if identification is not None else None
            ),
            elapsed_ms=elapsed_ms,
            timing=timing,
        )

    async def _discard_voice_join(
        self,
        utterance_id: str,
        *,
        cancel: bool = True,
    ) -> None:
        # 运行于：识别/接收循环 Task；持锁：否；可能阻塞：cancel=True 时等待声纹任务取消清理，未设本地总超时。
        state = self._voice_joins.pop(utterance_id, None)
        if cancel and state is not None and state.task is not None and not state.task.done():
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)

    async def _on_asr_error(self, utterance_id: str, error: Exception) -> None:
        # 运行于：识别 Task 或队列溢出时的接收循环 Task；持锁：否；可能阻塞：错误通知及声纹取消清理，未设总超时。
        if self._closing:
            return
        logger.warning(
            "asr_failed session_id=%s utterance_id=%s recognizer=%s error_type=%s",
            self.session_id,
            utterance_id,
            type(self._recognizer).__name__,
            type(error).__name__,
        )
        await self.send_json(
            {
                "type": "asr_failed",
                "utterance_id": utterance_id,
                "code": "recognition_failed",
                "message": "Unable to recognize speech",
            }
        )
        await self._discard_voice_join(utterance_id)

def _matches_input_format(value: Any) -> bool:
    return isinstance(value, dict) and value == {
        "codec": INPUT_AUDIO_FORMAT.codec,
        "sample_rate": INPUT_AUDIO_FORMAT.sample_rate,
        "channels": INPUT_AUDIO_FORMAT.channels,
        "frame_duration_ms": INPUT_AUDIO_FORMAT.frame_duration_ms,
    }


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
