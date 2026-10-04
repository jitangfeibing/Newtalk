from dataclasses import dataclass
import json
from typing import Literal


MemoryKind = Literal["fact", "preference", "event"]


class MemoryNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryItem:
    memory_id: str
    kind: MemoryKind
    title: str
    content: str
    relativity: float


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    items: tuple[MemoryItem, ...]

    def to_tool_content(self, *, max_chars: int) -> str:
        if max_chars <= 0:
            raise ValueError("Memory result budget must be positive")
        payload = {
            "status": "ok",
            "memories": [
                {
                    "type": item.kind,
                    "title": item.title,
                    "content": item.content,
                    "relativity": item.relativity,
                }
                for item in self.items
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= max_chars:
            return text
        return json.dumps(
            {
                "status": "truncated",
                "memories": [],
                "message": "检索结果超过字符预算，请基于当前上下文回答或向用户确认。",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )[:max_chars]


@dataclass(frozen=True, slots=True)
class MemoryWriteReceipt:
    provider_task_id: str | None
    status: str


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
