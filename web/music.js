import {createMediaPlayer} from './media-player.js?v=20261026.3';

export function initializeMusic() {
    const panel = document.querySelector('#mediaPanel'), title = document.querySelector('#mediaTitle');
    const artist = document.querySelector('#mediaArtist'), status = document.querySelector('#mediaStatus');
    const form = document.querySelector('#musicForm'), input = document.querySelector('#musicQuery');
    const feedback = document.querySelector('#musicFeedback'), play = document.querySelector('#musicPlayButton');
    const controls = [...document.querySelectorAll('[data-music-action]')];
    let sender = null, enabled = false, mediaEnabled = false;
    const player = createMediaPlayer({
        report: event => sender?.(event),
        onChange(state) {
            panel.hidden = !state.track && state.status === 'idle';
            title.textContent = state.target ? `正在查找：${state.target}` : state.track?.title || '音乐';
            artist.textContent = state.track ? [state.track.album, state.track.artist, state.track.source].filter(Boolean).join(' · ') : '';
            status.textContent = state.reason;
            for (const button of controls) {
                button.disabled = !sender || (!state.track && !state.target);
                const type = state.track?.media_type === 'podcast' ? '节目' : '音乐';
                button.textContent = {pause: `暂停${type}`, resume: '继续播放', stop: `停止${type}`}[button.dataset.musicAction];
            }
        },
    });
    function command(action, query = null) {
        if (!sender || !(action === 'play' ? enabled : mediaEnabled)) { feedback.textContent = '请先连接聊天并启用媒体服务。'; return; }
        if (action === 'play') {
            player.pause('正在切换歌曲'); feedback.textContent = '正在搜索并解析可播放歌曲…';
        }
        sender({type: 'music_control', event_id: crypto.randomUUID(), action, query, index: null});
    }
    form.addEventListener('submit', event => {
        event.preventDefault(); command('play', input.value.trim() || '轻音乐');
    });
    for (const button of controls) button.addEventListener('click', () => command(button.dataset.musicAction));
    document.querySelector('#musicVolume').addEventListener('input', event => player.volume(event.target.value));
    return {
        connect(send) { sender = send; player.connect(Boolean(send)); play.disabled = !send || !enabled; },
        capabilities(value, podcasts = false) {
            enabled = Boolean(value); mediaEnabled = enabled || Boolean(podcasts); play.disabled = !sender || !enabled;
            feedback.textContent = enabled ? '可按歌手、歌名或风格点播。部分歌曲暂不可播放。' : '音乐演示尚未配置。';
        },
        handle(event) {
            if (event.type === 'media_status') { player.operation(event); }
            else if (event.type === 'media_error') {
                if (player.error(event.message, event.revision)) feedback.textContent = event.message;
            }
            else { feedback.textContent = ''; player.handle(event); }
        },
        setBusy: player.setBusy,
        pause: player.pause,
    };
}
