import json
from dataclasses import dataclass
from typing import Literal

MemoryKind = Literal["fact", "preference", "event"]
MemoryWriteStatus = Literal["running", "completed", "failed"]


class MemoryNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryItem:
    memory_id: str
    kind: MemoryKind
    title: str
    content: str
    relativity: float
    created_at: str | None = None
    updated_at: str | None = None
    conversation_id: str | None = None
    confidence: float | None = None
    event_time: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    items: tuple[MemoryItem, ...]

    def deduplicated(self) -> "MemorySearchResult":
        seen_ids: set[str] = set()
        seen_content: set[
            tuple[MemoryKind, str, str | None, str | None, tuple[str, ...]]
        ] = set()
        items: list[MemoryItem] = []
        for item in self.items:
            if item.memory_id in seen_ids:
                continue
            seen_ids.add(item.memory_id)
            # 无时间证据的同文事件可能是多次发生，不能按正文合并。
            if item.kind == "event" and not item.event_time and not item.created_at:
                items.append(item)
                continue
            key = (
                item.kind,
                " ".join(item.content.split()),
                item.created_at,
                item.conversation_id,
                item.event_time,
            )
            if key not in seen_content:
                seen_content.add(key)
                items.append(item)
        return MemorySearchResult(tuple(items))

    def to_tool_content(self, *, max_chars: int) -> str:
        if max_chars <= 0:
            raise ValueError("Memory result budget must be positive")
        memories: list[dict[str, object]] = []
        for item in self.deduplicated().items:
            memory: dict[str, object] = {
                "memory_id": item.memory_id,
                "type": item.kind,
                "title": item.title,
                "content": item.content,
                "relativity": item.relativity,
            }
            for name in ("created_at", "updated_at", "conversation_id", "event_time"):
                value = getattr(item, name)
                if value:
                    memory[name] = value
            if item.confidence is not None:
                memory["confidence"] = item.confidence
            memories.append(memory)

        def serialize(selected: list[dict[str, object]], omitted_count: int = 0) -> str:
            payload: dict[str, object] = {
                "status": "truncated" if omitted_count else ("ok" if selected else "no_match"),
                "memories": selected,
            }
            if omitted_count:
                payload["omitted_count"] = omitted_count
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        text = serialize(memories)
        if len(text) <= max_chars:
            return text
        if len(serialize([], len(memories))) > max_chars:
            raise ValueError("Memory result budget cannot fit the minimal status object")

        selected: list[dict[str, object]] = []
        for memory in memories:
            candidate = selected + [memory]
            # 预算包含省略状态；长条放不下时继续尝试后续候选，不截断原文。
            if len(serialize(candidate, len(memories) - len(candidate))) <= max_chars:
                selected.append(memory)
        return serialize(selected, len(memories) - len(selected))


@dataclass(frozen=True, slots=True)
class MemoryWriteReceipt:
    provider_task_id: str | None
    status: MemoryWriteStatus


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    kind: MemoryKind
    title: str
    content: str
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryPage:
    items: tuple[MemoryRecord, ...]
    page: int
    size: int
    total: int
    pages: int
