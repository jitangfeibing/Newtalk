from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from newtalk.identity.api import require_device
from newtalk.identity.models import Device


router = APIRouter(prefix="/api/debug/context", tags=["context-debug"])
PRIVATE_HEADERS = {"Cache-Control": "no-store"}


def _summaries(events: list[dict]) -> list[dict]:
    turns: dict[str, dict] = {}
    for event in events:
        turn_id = event.get("turn_id")
        if not turn_id:
            continue
        summary = turns.setdefault(turn_id, {
            "turn_id": turn_id, "at": event["at"], "speaker": "未记录",
            "preview": "上下文片段（首部未记录或已淘汰）", "source": "unknown",
            "outcome": "running", "request_count": 0, "has_context": False,
        })
        if event["event"] == "context_selected":
            summary.update(speaker=event["identity"]["display_name"],
                           source=event.get("source", "unknown"), has_context=True)
            if event.get("messages"):
                text = event["messages"][-1]["content"]
                if text.startswith("[说话人："):
                    text = text.split("\n", 1)[-1]
                summary["preview"] = text[:120]
        elif event["event"] == "llm_request":
            summary["request_count"] += 1
        elif event["event"] == "turn_end":
            summary["outcome"] = event["outcome"]
        elif event["event"] == "turn_cancelled":
            summary["outcome"] = "cancelled"
    return sorted(turns.values(), key=lambda item: item["at"], reverse=True)


@router.get("")
async def list_context_turns(
    request: Request, response: Response,
    limit: int = Query(default=30, ge=1, le=50),
    device: Device = Depends(require_device),
) -> dict:
    response.headers.update(PRIVATE_HEADERS)
    debugger = request.app.state.chat_service.context_debug
    return {
        "enabled": debugger.enabled,
        "retention_seconds": debugger.retention_seconds,
        "scope": "current_device_current_process",
        "turns": _summaries(debugger.events_for_device(device.device_id))[:limit],
    }


@router.get("/{turn_id}")
async def get_context_turn(
    turn_id: str, request: Request, response: Response,
    device: Device = Depends(require_device),
) -> dict:
    response.headers.update(PRIVATE_HEADERS)
    debugger = request.app.state.chat_service.context_debug
    if not debugger.enabled:
        raise HTTPException(503, "Context debug is disabled", headers=PRIVATE_HEADERS)
    # Scope 只能来自 Cookie 鉴权；猜到其他设备的 turn_id 也不能读取。
    events = debugger.events_for_device(device.device_id, turn_id)
    if not events:
        raise HTTPException(404, "Context snapshot is unavailable", headers=PRIVATE_HEADERS)
    return {"turn_id": turn_id, "events": events}
