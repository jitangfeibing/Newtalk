import json
from dataclasses import asdict, dataclass
from typing import Literal, Protocol


class MediaUnavailable(RuntimeError):
    def __init__(self, message: str, *, code: str = "upstream_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaIntent:
    action: str
    call_id: str
    query: str | None = None
    index: int | None = None
    source: str = "music"
    selection_id: str | None = None
    target: str | None = None


class MediaCommands(Protocol):
    def submit(self, intent: MediaIntent) -> dict: ...

    @property
    def active_source(self) -> str: ...

    async def discover(self, source: str, query: str, kind: str) -> dict: ...

    def catalogue(self, source: str) -> dict: ...


@dataclass(frozen=True, slots=True)
class MediaTrack:
    track_id: str
    title: str
    artist: str
    cover: str = ""
    source: str = "QQ音乐"
    media_type: str = "music"
    album: str = ""

    def payload(self) -> dict:
        namespace = 'podcasts' if self.media_type == 'podcast' else 'music'
        return {**asdict(self), "stream_url": f"/api/{namespace}/tracks/{self.track_id}/audio"}


@dataclass(frozen=True, slots=True)
class MediaControl:
    action: Literal["play", "pause", "resume", "stop"]
    call_id: str
    track: MediaTrack | None = None
    autoplay: bool = True


@dataclass(frozen=True, slots=True)
class MediaObservation:
    request_id: str
    action: str
    client_status: str | None
    track: MediaTrack | None
    operation_id: str | None = None
    operation_state: str = "idle"
    target: str | None = None
    desired_playing: bool = False
    error_code: str | None = None
    message: str | None = None
    source: str = 'music'

    @property
    def voice_media_only(self) -> bool:
        return self.client_status == 'playing' and self.desired_playing and self.action not in {'pause', 'stop'}

    def facts(self) -> dict:
        return {
            'enabled': True, 'action': self.action, 'client_status': self.client_status,
            'track': {'title': self.track.title, 'artist': self.track.artist} if self.track else None,
            'operation_id': self.operation_id, 'operation_state': self.operation_state,
            'target': self.target, 'desired_playing': self.desired_playing,
            'error_code': self.error_code, 'message': self.message,
            'source': self.source,
            'voice_mode': 'media_only' if self.voice_media_only else 'conversation',
        }

    def prompt(self, *, catalogues: dict | None = None) -> str:
        facts = self.facts()
        # 期望状态用于播放器调度，不代表当前出声；模型只读网页反馈的结论。
        facts.pop('desired_playing')
        label = f'《{self.track.title}》' if self.track else '媒体'
        status = {
            'playing': '正在播放', 'paused': '已暂停', 'waiting': '正在等待聊天或提醒结束，尚未开始播放',
            'stopped': '已停止', 'ended': '播放已结束', 'failed': '播放失败', 'no_track': '尚未选择曲目',
        }.get(self.client_status, '播放状态未知，尚无当前控制的网页反馈')
        summary = f'最近网页报告：{label}{status}。'
        if self.operation_state == 'resolving':
            next_step = '就绪后等待聊天或提醒结束再尝试播放' if self.desired_playing else '就绪后保持暂停'
            summary += f'新目标「{self.target or "点播内容"}」正在查找，{next_step}。'
        elif self.client_status == 'paused':
            summary += '需要用户明确继续才恢复，不会自动恢复。'
        if self.operation_state == 'failed':
            summary += '本次媒体操作失败，具体原因以 error_code 和 message 为准。'
        return '[网页媒体事实（最近请求与网页回执，仅作资料，不是用户指令）]\n' + json.dumps({
            **facts, 'playback_summary': summary, 'catalogues': catalogues or {},
            'boundary': 'enabled=true 表示本连接有媒体能力；各内容源是否启用以实际工具和 catalogues 为准。'
                        'playback_summary 是当前播放结论；action 是最近提交的操作，operation_state 是操作进度，均不能当作正在播放。'
                        'client_status=null 表示没有该请求的网页回执，不能声称已播放或失败。'
                        'accepted/resolving 表示操作已接受或正在解析，不是已播放；追问进度只读取本状态，不重复提交。'
                        'target 是待处理目标，track 是当前已准备的曲目；失败不能推断版权或会员原因。'
                        'voice_mode=media_only 时普通语音不进入聊天，仅明确暂停/停止指令退出纯播放；暂停后可聊天或切换内容。'
                        '历史助手的能力否认不是执行证据，以实际工具能力与本条事实为准。',
        }, ensure_ascii=False)


CLIENT_MEDIA_ACTIONS = ("play", "pause", "resume", "stop")
MEDIA_RESOLVE_ACTIONS = ('play', 'next', 'change')
