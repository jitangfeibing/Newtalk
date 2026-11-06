"""持久化素材与后台整理编排；与实时回复和远端投递 Task 分开。"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

from newtalk.chat.models import Turn
from newtalk.memory.batch_store import MemoryBatch, MemorySource
from newtalk.memory.jobs import MemoryWriteService
from newtalk.memory.policy import memory_skip_reason

logger = logging.getLogger(__name__)
_BATCH_INPUT_LIMIT = 8
_BATCH_MAX_AGE_SECONDS = 300


def _prepared_payload(batch: MemoryBatch, result: dict) -> dict:
    source_ids = [source.turn_id for source in batch.sources if not source.context_only]
    observed = max(source.chat_time for source in batch.sources if not source.context_only)
    referenced = {turn_id for memory in result["memories"] for turn_id in memory["source_turn_ids"]}
    kinds = {memory["kind"] for memory in result["memories"]}
    # MemOS 可能接收 system-only 消息却不抽取；只附候选引用的真实用户原文供核验。
    originals = [{"role": "user", "role_name": source.speaker_name, "content": source.user_text,
                  "chat_time": source.chat_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")}
                 for source in batch.sources if source.turn_id in referenced]
    return {
        "messages": [{"role": "system", "chat_time": observed.astimezone(
            timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"), "content": (
            "[后台整理的用户记忆候选，不是用户原话；evidence 仅用于溯源]\n"
            + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )}, *originals],
        "info": {"batch_id": batch.batch_id, "device_id": batch.device_id,
                 "identity_id": batch.identity_id, "source_turn_ids": source_ids,
                 "source_type": "newtalk_curated_batch", "curation_policy_version": 1},
        # 不仅靠提示词：事件批次不能触发云端偏好/事实二次抽取。
        "allow_memory_view": [view for kind, view in (
            ("fact", "detail_factual"), ("preference", "preference"),
            ("fact", "profile"), ("event", "event")) if kind in kinds],
        "custom_extract_prompt": {
            "detail_factual": "只提取系统整理 memories 中 kind=fact 且被后续用户原文支持的内容。evidence 和用户原文只供核验，不额外重复抽取。不要把计划、推测或助手回答变为既成事实。",
            "preference": "只提取 memories 中 kind=preference 的明确用户偏好。不要从一次事件、计划或事实推断新偏好。",
            "profile": "只使用 memories 中有明确依据的稳定个人事实，遵守已有字段锁定；不要将事件、短期目标或心情推断成画像。",
            "event": "只提取 memories 中 kind=event 的事件。保留 state 与 event_time；unknown 不补成已完成，planned 不当成已发生。不同日期事件分开，来源 observed_at 不是事件时间。evidence 只溯源，不额外生成对话过程记忆。",
        },
    }


class MemoryBatchService:
    def __init__(
        self, store, curator, writer: MemoryWriteService, *, poll_seconds: float = 1,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC), max_attempts: int = 3,
    ) -> None:
        if poll_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Memory batch polling and retry limits must be positive")
        self.store = store
        self.writer = writer
        self._curator = curator
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        await self.store.start()
        self._worker = asyncio.create_task(self._run())

    async def record_input(self, turn: Turn, *, segment_id: str) -> bool:
        if turn.speaker_identity_id is None or memory_skip_reason(turn.user_text) is not None:
            return False
        inserted = await self.store.append(MemorySource(
            turn_id=turn.turn_id, device_id=turn.device_id, identity_id=turn.speaker_identity_id,
            session_id=turn.session_id, segment_id=segment_id,
            speaker_name=turn.speaker_display_name, user_text=turn.user_text, chat_time=turn.created_at,
        ))
        logger.info("memory_source_recorded turn_id=%s identity_id=%s inserted=%s",
                    turn.turn_id, turn.speaker_identity_id, inserted)
        return inserted

    async def finish_input(self, turn: Turn, *, assistant_text: str | None = None, discard: bool = False) -> None:
        await self.store.finish(turn.turn_id, assistant_text, discard=discard)
        self._wake.set()

    async def flush_segment(self, *, device_id: str, segment_id: str, reason: str) -> int:
        # 仅封存数据库快照；连接关闭不等待模型整理或 MemOS。
        sealed = await self.store.seal(device_id=device_id, segment_id=segment_id, reason=reason)
        logger.info("memory_segment_sealed device_id=%s segment_id=%s reason=%s batches=%s",
                    device_id, segment_id, reason, sealed)
        self._wake.set()
        return sealed

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None:
        await self.store.delete_identity(device_id=device_id, identity_id=identity_id)

    async def _tick(self) -> bool:
        await self.store.seal_due(max_sources=_BATCH_INPUT_LIMIT, max_age_seconds=_BATCH_MAX_AGE_SECONDS)
        batch = await self.store.claim()
        if batch is None:
            return False
        await self._process(batch)
        return True

    async def _process(self, batch: MemoryBatch) -> None:
        try:
            payload = batch.prepared_payload
            if payload is None:
                result = await self._curator.curate(batch)
                if not result["memories"]:
                    await self.store.skip(batch)
                    logger.info("memory_batch_skipped batch_id=%s reason=no_durable_memory", batch.batch_id)
                    return
                payload = _prepared_payload(batch, result)
                if not await self.store.save_prepared(batch, payload):
                    return
            # 整理结果先落库。领取租约/进程恢复后复用相同结果和确定 batch_id。
            await self.writer.enqueue_prepared_batch(
                batch_id=batch.batch_id, device_id=batch.device_id, identity_id=batch.identity_id,
                session_id=batch.session_id, speaker_name=batch.speaker_name, prepared_payload=payload,
            )
            if await self.store.mark_queued(batch):
                logger.info("memory_batch_queued batch_id=%s identity_id=%s source_count=%s",
                            batch.batch_id, batch.identity_id,
                            sum(not item.context_only for item in batch.sources))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- 后台任务失败需持久化重试，异常正文可能包含私人素材。
            retry_at = (self._clock() + timedelta(seconds=min(2 ** batch.attempts, 30))
                        if batch.attempts < self._max_attempts else None)
            # 模型/供应商错误可能含私人正文，只存错误类型，不复制异常文本。
            await self.store.fail(batch, error=type(exc).__name__, retry_at=retry_at)
            logger.warning("memory_batch_failed batch_id=%s attempts=%s retry=%s error_type=%s",
                           batch.batch_id, batch.attempts, retry_at is not None, type(exc).__name__)

    async def _run(self) -> None:
        while True:
            try:
                if await self._tick():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- 单次数据库故障不能结束 worker，取消另行传播。
                logger.error("memory_batch_worker_failed error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), self._poll_seconds)
            except TimeoutError:
                pass
            self._wake.clear()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        try:
            await self._curator.close()
        finally:
            await self.store.close()
