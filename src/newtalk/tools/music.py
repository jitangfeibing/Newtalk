from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from newtalk.media.models import MediaIntent, MediaUnavailable
from newtalk.tools.models import ToolContext, tool_error

logger = logging.getLogger(__name__)
_EXPLICIT_CONTROLS = {
    "暂停音乐": "pause", "暂停播放音乐": "pause",
    "继续音乐": "resume", "继续播放音乐": "resume", "恢复音乐": "resume",
    "停止音乐": "stop", "停止播放音乐": "stop",
}
_MUSIC_REQUEST = re.compile(
    r"播放|点播|放(?:一首|首|点|段)|来(?:一首|首)|(?:想|要|还想|再)听(?!你|您)"
    r"|(?:搜(?:索)?|找|查找).*(?:音乐|歌|曲)"
    r"|(?:暂停|停止|继续|恢复|关掉|关闭).{0,8}(?:音乐|歌|播放|听(?!你|您))"
    r"|(?:换|切).{0,8}(?:首|歌|曲)"
)


def _music_requested(text: str) -> bool:
    # 历史点播和本轮的歌曲评价不能授权再次播放；执行权限只来自本轮操作表达。
    text = text.strip().rstrip("。.!！?？").strip()
    return bool(_MUSIC_REQUEST.search(text) or re.fullmatch(r"(?:请)?(?:继续|暂停(?:一下)?|停一下|听歌|再听一遍)(?:吧)?", text))

if TYPE_CHECKING:
    from newtalk.chat.models import ModelToolCall
    from newtalk.music.service import MusicService


@dataclass(frozen=True, slots=True)
class MusicTool:
    service: MusicService
    name = "music_control"

    def definition(self, context: ToolContext) -> dict | None:
        if not context.client_tools_enabled or not self.service.enabled or not _music_requested(context.turn.user_text):
            return None
        if (getattr(context.media_commands, 'active_source', 'music') == 'podcast'
                and not re.search(r'音乐|歌曲|歌手|换歌|切歌', context.turn.user_text)
                and re.search(r'暂停|继续|恢复|停止|关掉|关闭', context.turn.user_text)):
            return None
        return {"type": "function", "function": {
            "name": self.name, "strict": True,
            "description": (
                "只处理歌曲；有声书、小说、播客和主题节目使用听书工具。只执行当前这句话中新提出的音乐操作，不补做历史点播。"
                "当前用户明确要求播放、搜索音乐，或暂停/继续/停止音乐时调用。"
                "'我喜欢这首歌'、评价歌曲、问候、想聊天均应正常对话，不调用此工具。"
                "明确点播时必须实际调用工具，不能仅口头答应已播放；普通聊天直接回答。query 填歌手、歌名或风格，"
                "如'周杰伦 晴天'；未指定偏好可填'轻音乐'。play 提交独立点播任务，accepted 仅表示已接收、正在处理。"
                "search 仅列出候选；选择刚才结果的序号时 play + index，query 填 null。"
                "pause/resume/stop 的 query 和 index 都填 null；不需要的参数填 null。"
                "例如'暂停音乐'必须用 pause，'继续播放音乐'用 resume，'停止音乐'用 stop。"
                "成功结果仅交给网页尝试播放，是否真正出声以网页反馈为准。不要把聊天停止当成音乐停止。"
            ),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["search", "play", "pause", "resume", "stop"]},
                "query": {"type": ["string", "null"]},
                "index": {"type": ["integer", "null"]},
            }, "required": ["action", "query", "index"], "additionalProperties": False},
        }}

    async def execute(self, context: ToolContext, call: ModelToolCall) -> str:
        if not _music_requested(context.turn.user_text):
            logger.info("music_tool_skipped turn_id=%s reason=no_current_request", context.turn.turn_id)
            return json.dumps({
                "status": "ok", "action": "none", "reason": "not_requested", "enabled": self.service.enabled,
                "message": "本次无需新的音乐操作，音乐功能仍可用。这不是播放失败，也不能据此推断之前没有播放。",
            }, ensure_ascii=False)
        try:
            args = json.loads(call.arguments)
            if not isinstance(args, dict) or set(args) != {"action", "query", "index"}:
                raise ValueError("音乐工具参数无效。")
            # 明确控制口令优先于模型参数，避免把暂停误判为重新点播；不匹配泛指聊天的“停止”。
            explicit = _EXPLICIT_CONTROLS.get(context.turn.user_text.strip().rstrip("。.!！?？"))
            if explicit is not None:
                args = {"action": explicit, "query": None, "index": None}
            logger.info("music_tool_requested turn_id=%s call_id=%s action=%s", context.turn.turn_id, call.call_id,
                        args["action"] if args["action"] in ("search", "play", "pause", "resume", "stop") else "invalid")
            query = args["query"]
            if query is not None and not isinstance(query, str):
                raise ValueError("歌手或歌名必须是文字。")
            if args["action"] == "search":
                if args["index"] is not None:
                    raise ValueError("搜索时无需序号。")
                songs = await self.service.search(context.turn.device_id, query or "轻音乐")
                return json.dumps({"status": "ok", "songs": [song.payload() for song in songs]}, ensure_ascii=False)
            if context.media_commands is None:
                raise MediaUnavailable("当前没有可用的播放器连接。", code="unavailable")
            result = context.media_commands.submit(MediaIntent(args["action"], call.call_id, query, args["index"]))
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, MediaUnavailable, TimeoutError) as exc:
            return tool_error("music_unavailable", str(exc) or "音乐请求超时，请稍后重试。")
