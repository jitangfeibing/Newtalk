import asyncio
import json

import pytest

from newtalk.media.models import (
    MediaControl,
    MediaIntent,
    MediaObservation,
    MediaTrack,
    MediaUnavailable,
)
from newtalk.media.session import MediaSession

TRACK = MediaTrack('11111111-1111-1111-1111-111111111111', '测试曲', '歌手')


class Source:
    enabled = True

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = []

    async def command(self, device, action, query, index, call_id):
        self.calls.append((action, query, index, call_id))
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return MediaControl('play', call_id, TRACK)


class Harness:
    def __init__(self, source=None, **kwargs):
        self.source = source or Source()
        self.events = []
        self.queue = asyncio.Queue()
        self.session = MediaSession(device_id='device', resolvers={'music': self.source}, emit=self.emit, **kwargs)

    async def emit(self, event):
        self.events.append(event)
        self.queue.put_nowait(event)

    async def next(self, kind):
        async def receive():
            while True:
                event = await self.queue.get()
                if event['type'] == kind:
                    return event
        return await asyncio.wait_for(receive(), 1)


def test_accepted_operation_is_immediate_idempotent_and_independent_of_submitter():
    async def scenario():
        h = Harness()
        try:
            intent = MediaIntent('play', 'call', '歌手 歌曲')
            accepted = h.session.submit(intent)
            assert accepted['status'] == 'accepted'
            assert accepted == h.session.submit(intent)
            await asyncio.wait_for(h.source.entered.wait(), 1)
            assert len(h.source.calls) == 1
            assert h.session.observation.operation_state == 'resolving'
            assert h.session.observation.client_status is None
            with pytest.raises(ValueError):
                h.session.submit(MediaIntent('stop', 'call'))
            h.source.release.set()
            control = await h.next('media_control')
            assert control['operation_id'] == accepted['operation_id']
            assert control['autoplay'] is True and 'turn_id' not in control
            assert h.session.observation.client_status is None
            assert h.session.report({**control, 'status': 'playing'})
            assert h.session.observation.client_status == 'playing'
        finally:
            await h.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('resume', [False, True])
def test_pause_and_resume_during_lookup_reuse_the_same_operation(resume):
    async def scenario():
        h = Harness()
        try:
            accepted = h.session.submit(MediaIntent('play', 'play', '歌手'))
            await asyncio.wait_for(h.source.entered.wait(), 1)
            paused = h.session.submit(MediaIntent('pause', 'pause'))
            assert paused['operation_id'] == accepted['operation_id']
            if resume:
                h.session.submit(MediaIntent('resume', 'resume'))
            h.source.release.set()
            control = await h.next('media_control')
            assert control['action'] == 'play' and control['autoplay'] is resume
            assert not h.source.cancelled.is_set() and len(h.source.calls) == 1
        finally:
            await h.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('replacement', ['play', 'stop', 'disconnect'])
@pytest.mark.parametrize('late_failure', [False, True])
def test_late_result_or_error_cannot_revive_a_superseded_operation(replacement, late_failure):
    async def scenario():
        old_entered, old_cancelled, old_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old_tasks = []

        class LateSource(Source):
            async def command(self, device, action, query, index, call_id):
                if query != 'old':
                    return MediaControl('play', call_id, TRACK)
                old_tasks.append(asyncio.current_task())
                old_entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    old_cancelled.set()
                    await old_release.wait()
                if late_failure:
                    raise MediaUnavailable('旧请求失败', code='no_resource')
                return MediaControl('play', call_id, TRACK)

        h = Harness(LateSource())
        h.session.submit(MediaIntent('play', 'old', 'old'))
        await asyncio.wait_for(old_entered.wait(), 1)
        closing = None
        try:
            if replacement == 'disconnect':
                closing = asyncio.create_task(h.session.close())
            else:
                h.session.submit(MediaIntent(replacement, 'new', 'new' if replacement == 'play' else None))
            await asyncio.wait_for(old_cancelled.wait(), 1)
            old_release.set()
            await asyncio.wait_for(old_tasks[0], 1)
            if closing:
                await asyncio.wait_for(closing, 1)
                assert not any(event['type'] in {'media_control', 'media_error'} for event in h.events)
                assert not h.session.task_count
            else:
                control = await h.next('media_control')
                assert control['action'] == replacement
                assert not any(event['type'] == 'media_error' for event in h.events)
                assert len([event for event in h.events if event['type'] == 'media_control']) == 1
        finally:
            old_release.set()
            await h.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,code', [
    (MediaUnavailable('没有地址', code='no_resource'), 'no_resource'),
    (RuntimeError('private-provider-details'), 'internal_error'),
    (None, 'timeout'),
])
def test_resolution_failures_have_explicit_categories_without_guessing_membership(failure, code):
    async def scenario():
        class FailedSource(Source):
            async def command(self, *args):
                if failure is not None:
                    raise failure
                await asyncio.Future()

        h = Harness(FailedSource(), resolve_timeout=0.01)
        try:
            h.session.submit(MediaIntent('play', 'call', '歌手'))
            error = await h.next('media_error')
            assert error['code'] == code
            assert '会员' not in error['message'] and 'private-provider-details' not in error['message']
            assert h.session.observation.error_code == code
            assert h.session.observation.operation_state == 'failed'
        finally:
            await h.session.close()
    asyncio.run(scenario())


def test_player_state_continues_after_ack_and_old_feedback_cannot_replace_new_command():
    async def scenario():
        h = Harness()
        h.source.release.set()
        try:
            h.session.submit(MediaIntent('play', 'play', '歌手'))
            old = await h.next('media_control')
            assert h.session.report({**old, 'status': 'playing'})
            assert not h.session.report({**old, 'status': 'playing'})
            assert h.session.report_state({**old, 'status': 'paused'})
            assert h.session.observation.client_status == 'paused'
            assert h.session.report_state({**old, 'status': 'ended'})
            h.session.submit(MediaIntent('stop', 'stop'))
            current = await h.next('media_control')
            assert not h.session.report_state({**old, 'status': 'playing'})
            assert not h.session.report_state({**current, 'status': {}})
            assert not h.session.report({**current, 'action': 'play', 'status': 'playing'})
            assert h.session.report({**current, 'status': 'stopped'})
            assert h.session.observation.client_status == 'stopped'
        finally:
            await h.session.close()
        assert not h.session.pending_requests
        assert not h.session.report_state({**current, 'status': 'playing'})
    asyncio.run(scenario())


def test_cancelled_turn_cannot_submit_a_late_intent():
    async def scenario():
        h = Harness()
        try:
            commands = h.session.for_turn(lambda: False)
            with pytest.raises(MediaUnavailable) as caught:
                commands.submit(MediaIntent('play', 'call', '歌手'))
            assert caught.value.code == 'cancelled'
            assert not h.session.task_count and not h.source.calls
        finally:
            await h.session.close()
    asyncio.run(scenario())


def test_pending_feedback_is_bounded_and_overflow_has_an_explicit_control_failure():
    async def scenario():
        h = Harness()
        try:
            for index in range(16):
                h.session.submit(MediaIntent('pause', f'pause-{index}'))
                await h.next('media_control')
            assert len(h.session.pending_requests) == 16
            h.session.submit(MediaIntent('resume', 'overflow'))
            error = await h.next('media_error')
            assert error['code'] == 'busy'
            assert h.session.observation.operation_state == 'failed'
            assert len(h.session.pending_requests) == 16
        finally:
            await h.session.close()
    asyncio.run(scenario())


def test_model_snapshot_uses_reported_pause_instead_of_the_previous_play_intent():
    async def scenario():
        h = Harness()
        h.source.release.set()
        try:
            h.session.submit(MediaIntent('play', 'play', '演员'))
            control = await h.next('media_control')
            assert h.session.report({**control, 'status': 'playing'})
            assert h.session.report_state({**control, 'status': 'paused'})
            observation = h.session.observation
            assert observation.desired_playing is True
            snapshot = json.loads(observation.prompt().split('\n', 1)[1])
            assert snapshot['client_status'] == 'paused'
            assert '已暂停' in snapshot['playback_summary']
            assert '明确继续' in snapshot['playback_summary']
            assert 'desired_playing' not in snapshot
            assert observation.facts()['desired_playing'] is True
            assert len(h.source.calls) == 1
        finally:
            await h.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,expected', [
    (None, '未知'), ('waiting', '尚未开始播放'), ('playing', '正在播放'),
    ('ended', '已结束'), ('stopped', '已停止'), ('failed', '播放失败'),
])
def test_model_summary_distinguishes_browser_feedback_from_a_completed_resume_request(status, expected):
    observation = MediaObservation('request', 'resume', status, TRACK,
                                   operation_state='completed', desired_playing=True)
    snapshot = json.loads(observation.prompt().split('\n', 1)[1])
    assert expected in snapshot['playback_summary']
    if status != 'playing':
        assert '正在播放' not in snapshot['playback_summary']


def test_new_lookup_is_not_confused_with_the_previous_paused_track():
    observation = MediaObservation('old', 'play', 'paused', TRACK, operation_state='resolving',
                                   target='新点播歌曲', desired_playing=True)
    snapshot = json.loads(observation.prompt().split('\n', 1)[1])
    assert '已暂停' in snapshot['playback_summary']
    assert '新点播歌曲' in snapshot['playback_summary']
    assert '正在查找' in snapshot['playback_summary']
    assert '不会自动恢复' not in snapshot['playback_summary']
