from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from newtalk.media.models import MediaIntent, MediaUnavailable
from newtalk.podcasts.service import PodcastService
from newtalk.tools.models import ToolContext, tool_error

logger = logging.getLogger(__name__)
_CONTENT = re.compile(r'听书|有声|播客|喜马拉雅|节目|小说|故事|评书|相声|课程|专辑|聊球')
_PLAY = re.compile(r'播放|听|放(?:点|个|一个|一段)|来(?:点|个|一段)|第?[一二三四五六七八九十\d]+个|就(?:这|那|它)|换(?:一个|个|本)|下一(?:集|期)')


def _catalogue(context: ToolContext) -> dict:
    return context.media_commands.catalogue('podcast') if context.media_commands else {}


def _selection(text: str, catalogue: dict) -> bool:
    if not catalogue.get('albums'):
        return False
    name = re.sub(r'^(?:就)?(?:那个|这个)?', '', text.strip().rstrip('。.!！?？吧')).removesuffix('的')
    return bool(name and (name in {'聊球', '评论', '小说', '故事', '课程', '访谈', '随便', '你选', '你挑', '你来选', '都行', '都可以'} or
                (len(name) >= 2 and any(name in item['title'] or name in item['anchor']
                                       for item in catalogue['albums']))))


def _requested(context: ToolContext) -> bool:
    text = context.turn.user_text
    catalogue = _catalogue(context)
    active = context.media_commands and context.media_commands.active_source == 'podcast'
    return bool(_CONTENT.search(text) or _PLAY.search(text) or
                (active and re.search(r'暂停|停止|继续(?!聊|说|讲)|恢复|停一下', text)) or
                _selection(text, catalogue))


def _definition(name: str, description: str, properties: dict) -> dict:
    return {'type': 'function', 'function': {'name': name, 'strict': True, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': list(properties),
                           'additionalProperties': False}}}


@dataclass(frozen=True, slots=True)
class PodcastSearchTool:
    service: PodcastService
    name = 'podcast_search'
    followup_tools = ('podcast_control',)

    def definition(self, context: ToolContext) -> dict | None:
        if not self.service.enabled or not context.client_tools_enabled or not context.media_commands or not _requested(context):
            return None
        return _definition(self.name,
            '搜索有声书、小说、喜马拉雅专辑、播客和主题节目，不搜索歌曲。query 填主题或专辑名。'
            'kind：连续小说/课程用 series，独立聊球/访谈用 program，不确定用 auto。'
            '明确点播或泛听请求：先搜索，明显匹配时再调用 podcast_control 播放；泛听优先相关免费内容。'
            '有明显类型或同名版本歧义时，只问一个简短问题，最多介绍两个选项，不机械读长列表。'
            '只要求找节目/列候选时不自动播放。序号、简称选择优先使用网页媒体事实中的现有候选。'
            '候选仅有标题/主播等元数据，不编造内容质量、简介或最新发布时间。',
            {'query': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['auto', 'series', 'program']}})

    async def execute(self, context: ToolContext, call) -> str:
        try:
            if not _requested(context) or context.media_commands is None:
                raise ValueError('本轮没有听书搜索请求。')
            args = json.loads(call.arguments)
            if not isinstance(args, dict) or set(args) != {'query', 'kind'}:
                raise ValueError('听书搜索参数无效。')
            result = await context.media_commands.discover('podcast', args['query'], args['kind'])
            logger.info('podcast_tool_search turn_id=%s call_id=%s', context.turn.turn_id, call.call_id)
            return json.dumps(result, ensure_ascii=False)
        except (MediaUnavailable, ValueError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, MediaUnavailable) else ('timeout' if isinstance(exc, TimeoutError) else 'invalid_request')
            return tool_error(code, str(exc) or '听书搜索超时。')


@dataclass(frozen=True, slots=True)
class PodcastControlTool:
    service: PodcastService
    name = 'podcast_control'

    def definition(self, context: ToolContext) -> dict | None:
        if not self.service.enabled or not context.client_tools_enabled or not context.media_commands or not _requested(context):
            return None
        return _definition(self.name,
            '操作听书/节目，不操作歌曲。只执行本轮新请求，歌曲评价和普通聊天不操作媒体。'
            'play 选择真实搜索结果：index 填候选显示序号，selection_id 填对应搜索结果的真实编号。'
            '明确点播匹配后直接播放；泛听可帮助选一个；有明显歧义先澄清，最多两个候选。'
            'next=同专辑下一集/下一期；小说按集数递增，独立节目按网站列表向后；change=换一个候选专辑；pause/resume/stop=暂停/继续/停止。'
            '非 play 的 index/selection_id 填 null。连续内容从确认首集开始，独立节目试听一期，不保证最新。'
            'accepted 只代表独立任务已接受，简短确认，不假称已出声。',
            {'action': {'type': 'string', 'enum': ['play', 'next', 'change', 'pause', 'resume', 'stop']},
             'index': {'type': ['integer', 'null']}, 'selection_id': {'type': ['string', 'null']}})

    async def execute(self, context: ToolContext, call) -> str:
        try:
            if not _requested(context) or context.media_commands is None:
                return json.dumps({'status': 'ok', 'action': 'none', 'reason': 'not_requested'})
            args = json.loads(call.arguments)
            if not isinstance(args, dict) or set(args) != {'action', 'index', 'selection_id'}:
                raise ValueError('听书操作参数无效。')
            action, index, selection_id = args['action'], args['index'], args['selection_id']
            if action not in ('play', 'next', 'change', 'pause', 'resume', 'stop'):
                raise ValueError('不支持这个听书操作。')
            text = context.turn.user_text
            control_request = {'pause': r'暂停|停一下', 'resume': r'继续(?!聊|讲|说)|恢复|接着听',
                               'stop': r'停止|别放|关掉|关闭', 'next': r'下一(?:集|期)', 'change': r'换'}
            if action != 'play' and not re.search(control_request[action], text):
                return json.dumps({'status': 'ok', 'action': 'none', 'reason': 'not_requested'})
            # 纯查找没有授权出声；模型仍可在本轮正常介绍候选。
            search_only = re.search(r'找|搜|查|列', text) and not re.search(r'播放|放点|放个|听听|想听|要听', text)
            if action == 'play' and (search_only or not (_PLAY.search(text) or _selection(text, _catalogue(context)))):
                return json.dumps({'status': 'ok', 'action': 'none', 'reason': 'search_only'})
            target = None
            if action == 'play':
                facts = _catalogue(context)
                if (selection_id != facts.get('selection_id') or type(index) is not int
                        or not 1 <= index <= len(facts.get('albums', []))):
                    raise ValueError('请使用当前候选的真实序号和选择编号。')
                target = facts['albums'][index - 1]['title']
            elif index is not None or selection_id is not None:
                raise ValueError('这个控制动作不需要候选参数。')
            result = context.media_commands.submit(MediaIntent(action, call.call_id, index=index,
                                                               source='podcast', selection_id=selection_id, target=target))
            logger.info('podcast_tool_control turn_id=%s call_id=%s action=%s operation_id=%s',
                        context.turn.turn_id, call.call_id, action, result['operation_id'])
            return json.dumps(result, ensure_ascii=False)
        except (MediaUnavailable, ValueError) as exc:
            return tool_error(getattr(exc, 'code', 'invalid_request'), str(exc))
