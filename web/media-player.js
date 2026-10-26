// 媒体与聊天 PCM、提醒音频分开；被对话/提醒暂停后，只有明确继续才恢复。
export function createMediaPlayer({audio = new Audio(), report = () => {}, onChange = () => {}} = {}) {
    const busy = new Set(), seen = new Set();
    let connected = false, track = null, status = 'idle', reason = '', pending = null;
    let generation = 0, expectedSource = '', wantsPlay = false;
    let activeRequest = null, operation = null, revision = -1;
    audio.preload = 'none';

    function show(value, message = '') {
        status = value; reason = message;
        onChange({track, status, reason, target: operation?.operation_state === 'resolving' ? operation.target : null});
    }
    function reportState(value) {
        if (connected && activeRequest) report({type: 'media_state', request_id: activeRequest.request_id,
            action: activeRequest.action, status: value});
    }
    function reply(value) {
        if (!pending) return;
        report({type: 'media_result', request_id: pending.request_id, action: pending.action, status: value});
        if (value !== 'waiting') pending = null;
    }
    function pump() {
        if (!connected || !wantsPlay || !track || busy.size || !pending) return;
        const version = ++generation;
        if (!expectedSource) {
            audio.src = track.stream_url;
            expectedSource = audio.src;
        }
        show('loading', '正在加载音乐');
        Promise.resolve().then(() => {
            if (version !== generation || !connected || busy.size || !wantsPlay) return;
            return audio.play();
        }).catch(() => {
            if (version !== generation || !connected) return;
            wantsPlay = false; reply('failed');
            reportState('failed');
            show('failed', '浏览器未能播放，请点“继续播放”重试，或重新点播。');
        });
    }
    function interrupt(message = '已因聊天暂停，点击或说“继续播放音乐”恢复') {
        if (status !== 'playing' && status !== 'loading') return;
        if (status === 'loading' && pending && wantsPlay) {
            generation += 1; audio.pause(); show('waiting', '等待聊天或提醒播完'); reply('waiting'); return;
        }
        generation += 1; wantsPlay = false; audio.pause(); reply('paused');
        reportState('paused');
        show('paused', message);
    }
    function clearSource() {
        audio.pause(); audio.removeAttribute('src'); audio.load(); expectedSource = '';
    }
    audio.addEventListener('playing', () => {
        if (!connected || !track || !expectedSource || audio.currentSrc !== expectedSource) return;
        if (busy.size || !wantsPlay) { audio.pause(); return; }
        show('playing', '正在播放'); reply('playing'); reportState('playing');
    });
    audio.addEventListener('ended', () => {
        if (!connected || !track || !expectedSource || audio.currentSrc !== expectedSource) return;
        wantsPlay = false; show('ended', '播放结束'); reportState('ended');
    });
    audio.addEventListener('error', () => {
        if (!connected || !expectedSource || !['loading', 'playing'].includes(status)) return;
        wantsPlay = false; reply('failed'); reportState('failed'); show('failed', '音频源暂不可用，请重新点播或换一首。');
    });
    return {
        connect(value) {
            generation += 1; connected = Boolean(value);
            if (!connected) {
                pending = null; activeRequest = null; operation = null; revision = -1;
                wantsPlay = false; clearSource(); track = null;
                busy.clear(); seen.clear(); show('idle');
            }
        },
        handle(event) {
            if (!connected || typeof event.request_id !== 'string' || seen.has(event.request_id)) return;
            if (Number.isInteger(event.revision) && event.revision < revision) return;
            if (Number.isInteger(event.revision)) revision = event.revision;
            seen.add(event.request_id);
            if (seen.size > 256) seen.delete(seen.values().next().value);
            generation += 1;
            if (pending) reply('paused');
            pending = event;
            activeRequest = event;
            if (event.action === 'play') {
                if (!event.track || typeof event.track.title !== 'string'
                    || !/^\/api\/(?:music|podcasts)\/tracks\/[a-z0-9-]{36}\/audio$/.test(event.track.stream_url || '')) {
                    reply('failed'); return;
                }
                clearSource(); track = event.track; wantsPlay = event.autoplay !== false;
                if (!wantsPlay) { reply('paused'); show('paused', '资源已就绪，保持暂停'); return; }
            } else if (event.action === 'pause') {
                wantsPlay = false; audio.pause(); reply(track ? 'paused' : 'no_track');
                show(track ? 'paused' : 'idle', track ? '已暂停' : '尚未选择歌曲'); return;
            } else if (event.action === 'stop') {
                wantsPlay = false; clearSource(); reply('stopped'); show('stopped', '已停止'); return;
            } else if (event.action === 'resume') {
                if (!track) { reply('no_track'); show('idle', '请先点播一首歌曲'); return; }
                wantsPlay = true;
            } else { reply('failed'); return; }
            if (busy.size) { show('waiting', '等待聊天或提醒播完'); reply('waiting'); }
            else pump();
        },
        operation(event) {
            if (!connected || !Number.isInteger(event.revision) || event.revision < revision) return;
            const replaced = event.revision > revision;
            revision = event.revision; operation = event;
            if (event.operation_state === 'resolving') {
                // 新点播替代旧目标，不能在前台忙闲切换时恢复旧的待播请求。
                if (replaced) {
                    generation += 1; wantsPlay = false; audio.pause(); reply('paused');
                    reportState('paused');
                }
                interrupt('正在切换媒体'); show('resolving', event.desired_playing ? '正在查找播放资源…' : '正在查找，已设为暂停');
            } else if (event.operation_state === 'failed') {
                interrupt(); show('failed', event.message || '本次点播失败，请重新点播。');
            } else show(status, reason);
        },
        setBusy(key, value) {
            if (value) {
                busy.add(key);
                interrupt(key === 'reminder' ? '已因提醒暂停，提醒后请明确继续播放' : undefined);
            } else { busy.delete(key); pump(); }
        },
        pause: interrupt,
        volume(value) { audio.volume = Math.max(0, Math.min(1, Number(value) || 0)); },
        error(message, eventRevision) {
            if (!connected || (Number.isInteger(eventRevision) && eventRevision < revision)) return false;
            if (Number.isInteger(eventRevision)) revision = eventRevision;
            interrupt(); show('failed', message); return true;
        },
    };
}
