from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from newtalk.vision.provider import CameraFrame, QwenVisionProvider, VisionUnavailable

logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 512_000
MAX_EDGE = 1280


@dataclass(frozen=True, slots=True)
class _Commands:
    session: CameraSession
    turn_id: str
    is_current: Callable[[], bool]

    async def analyze(self, question: str) -> dict:
        return await self.session.analyze(self.turn_id, question, self.is_current)


class CameraSession:
    def __init__(self, provider: QwenVisionProvider, emit: Callable[..., Awaitable[None]]) -> None:
        self.provider = provider
        self._emit = emit
        self.opened = False
        self._camera_revision = 0
        self._closed = False
        self._request: tuple[str, str] | None = None
        self._is_current: Callable[[], bool] = lambda: False
        self._capture_deadline = 0.0
        self._frame: asyncio.Future[CameraFrame] | None = None
        self._operation: asyncio.Task[str] | None = None
        self.visual_turn_id: str | None = None

    def for_turn(self, turn_id: str, is_current: Callable[[], bool]) -> _Commands:
        return _Commands(self, turn_id, is_current)

    def state(self, event: dict) -> None:
        if type(event.get("opened")) is not bool or self._closed:
            return
        self.opened = event["opened"]
        revision = event.get("revision", 0)
        if type(revision) is int and 0 <= revision <= 2**53 - 1:
            self._camera_revision = revision
        # 关闭摄像头立即撤销当前看图操作，避免关掉预览后仍返回迟到的观察。
        if not self.opened and self._operation is not None:
            self._operation.cancel()

    def frame(self, event: dict) -> bool:
        if (not self.opened or self._closed or self._request is None
                or not self._is_current() or time.monotonic() > self._capture_deadline
                or (event.get("request_id"), event.get("turn_id")) != self._request
                or self._frame is None or self._frame.done()):
            return False
        try:
            if event.get("status") == "error":
                raise VisionUnavailable("capture_failed")
            encoded = event.get("image_base64")
            if (event.get("mime_type") != "image/jpeg" or not isinstance(encoded, str)
                    or len(encoded) > ((MAX_FRAME_BYTES + 2) // 3) * 4):
                raise VisionUnavailable("invalid_frame")
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_FRAME_BYTES:
                raise VisionUnavailable("invalid_frame")
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if (image.format != "JPEG" or not 1 <= width <= MAX_EDGE
                        or not 1 <= height <= MAX_EDGE):
                    raise VisionUnavailable("invalid_frame")
                image.load()
            self._frame.set_result(CameraFrame(raw, width, height))
        except (binascii.Error, ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            self._frame.set_exception(VisionUnavailable("invalid_frame"))
        except VisionUnavailable as exc:
            self._frame.set_exception(exc)
        return True

    async def analyze(self, turn_id: str, question: str, is_current: Callable[[], bool]) -> dict:
        request_id = str(uuid4())
        started = time.monotonic()
        outcome = "cancelled"
        try:
            if self._closed or not is_current():
                raise asyncio.CancelledError
            self.visual_turn_id = turn_id
            if not self.opened:
                raise VisionUnavailable("camera_off")
            if self._operation is not None:
                raise VisionUnavailable("busy")
            self._request = request_id, turn_id
            self._is_current = is_current
            self._capture_deadline = time.monotonic() + min(3, self.provider.timeout_seconds)
            self._frame = asyncio.get_running_loop().create_future()
            operation = asyncio.create_task(self._run(request_id, turn_id, question))
            self._operation = operation
            try:
                async with asyncio.timeout(self.provider.timeout_seconds):
                    observation = await operation
            except asyncio.CancelledError:
                # 父 Turn 取消必须继续向上传播；摄像头主动关闭则回到当前聊天说明不可见。
                if asyncio.current_task().cancelling() or self._closed:
                    raise
                raise VisionUnavailable("camera_off") from None
            finally:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                if self._frame is not None:
                    if self._frame.done() and not self._frame.cancelled():
                        self._frame.exception()
                    else:
                        self._frame.cancel()
                self._operation = None
                self._frame = None
                self._request = None
                self._is_current = lambda: False
            if not is_current() or self._closed:
                raise asyncio.CancelledError
            outcome = "ok"
            await self._status(request_id, turn_id, "completed")
            return {"status": "ok", "observed": True, "observation": observation,
                    "message": "这是本次单帧的观察资料，由你结合用户问题回答；不要把它当作持续实时画面。"}
        except (TimeoutError, VisionUnavailable) as exc:
            outcome = exc.code if isinstance(exc, VisionUnavailable) else "timeout"
            await self._status(request_id, turn_id, "unavailable", code=outcome)
            return {"status": "unavailable", "observed": False, "code": outcome,
                    "message": "这次没能看到画面。请如实说明，不能用旧画面或猜测代替；摄像头关闭时请用户点开启摄像头。"}
        finally:
            logger.info("vision_operation turn_id=%s request_id=%s outcome=%s elapsed_ms=%.1f",
                        turn_id, request_id, outcome, (time.monotonic() - started) * 1000)

    async def _run(self, request_id: str, turn_id: str, question: str) -> str:
        await self._status(request_id, turn_id, "capturing")
        await self._emit({"type": "camera_capture", "request_id": request_id, "turn_id": turn_id,
                          "camera_revision": self._camera_revision,
                          "max_edge": MAX_EDGE, "max_bytes": MAX_FRAME_BYTES,
                          "expires_at": int((time.time() + 3) * 1000)}, turn_id=turn_id)
        async with asyncio.timeout(min(3, self.provider.timeout_seconds)):
            frame = await self._frame
        await self._status(request_id, turn_id, "analyzing")
        return await self.provider.analyze(frame, question)

    async def _status(self, request_id: str, turn_id: str, status: str, **fields) -> None:
        await self._emit({"type": "vision_status", "request_id": request_id,
                          "turn_id": turn_id, "status": status, **fields}, turn_id=turn_id)

    async def close(self) -> None:
        self._closed = True
        self.opened = False
        if self._operation is not None:
            self._operation.cancel()
            await asyncio.gather(self._operation, return_exceptions=True)
