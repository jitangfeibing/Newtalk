import pytest

from newtalk.media.interaction import playback_stop_from_text


@pytest.mark.parametrize('text,action', [
    ('暂停', 'pause'), ('请暂停播放。', 'pause'), ('先暂停一下音乐', 'pause'),
    ('停一下', 'pause'), ('停止播放', 'stop'), ('停止节目！', 'stop'), ('关掉音乐', 'stop'),
])
def test_complete_pause_or_stop_commands(text, action):
    assert playback_stop_from_text(text) == action


@pytest.mark.parametrize('text', ['不要暂停', '暂停音乐吗？', '他说“停止播放”', '暂停是什么意思',
                                '如果暂停会怎样', '暂停然后换下一集', '继续播放', '下一集', ''])
def test_mentions_negations_questions_and_unrelated_actions_do_not_stop_playback(text):
    assert playback_stop_from_text(text) is None
