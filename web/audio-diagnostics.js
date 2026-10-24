import {pcmToWav} from './voiceprint-recorder.js?v=20261023.2';

const MAX_SAMPLES = 16000 * 10;

export class DiagnosticRecording {
    constructor() {
        this.clear();
    }

    start() {
        this.clear();
        this.active = true;
    }

    push(buffer) {
        if (!this.active) return;
        const available = MAX_SAMPLES - this.sampleCount;
        // 必须复制：这里只旁路观察发送帧，不能修改原始音频或持有无限录音。
        const frame = new Int16Array(buffer).slice(0, available);
        this.frames.push(frame);
        this.sampleCount += frame.length;
        if (this.sampleCount >= MAX_SAMPLES) this.active = false;
    }

    finish() {
        const blob = this.sampleCount ? pcmToWav(this.frames) : null;
        this.clear();
        return blob;
    }

    clear() {
        this.active = false;
        this.frames = [];
        this.sampleCount = 0;
    }
}

export function initializeAudioDiagnostics({isMicrophoneActive, beforePlayback}) {
    const recording = new DiagnosticRecording();
    const start = document.querySelector('#diagnosticRecordStart');
    const stop = document.querySelector('#diagnosticRecordStop');
    const clear = document.querySelector('#diagnosticRecordClear');
    const player = document.querySelector('#diagnosticRecordingPlayer');
    const download = document.querySelector('#diagnosticRecordingDownload');
    const status = document.querySelector('#diagnosticRecordingStatus');
    let url = null;
    let timer = null;
    let capturing = false;
    let resumingPlayback = false;

    function releasePreview() {
        player.pause();
        player.removeAttribute('src');
        player.load();
        player.hidden = true;
        download.removeAttribute('href');
        download.hidden = true;
        if (url) URL.revokeObjectURL(url);
        url = null;
    }

    function finish() {
        if (!capturing) return;
        capturing = false;
        window.clearTimeout(timer);
        timer = null;
        start.disabled = false;
        stop.disabled = true;
        const seconds = recording.sampleCount / 16000;
        const blob = recording.finish();
        if (!blob) {
            clear.disabled = true;
            status.textContent = '没有收到发送帧，请先连接并开启麦克风。';
            return;
        }
        url = URL.createObjectURL(blob);
        player.src = url;
        player.hidden = false;
        download.href = url;
        download.download = `newtalk-microphone-${Date.now()}.wav`;
        download.hidden = false;
        status.textContent = `已保存 ${seconds.toFixed(2)} 秒，16kHz / 单声道 / PCM16。试听会关闭聊天麦克风，避免回声触发新对话。`;
    }

    function reset() {
        capturing = false;
        window.clearTimeout(timer);
        timer = null;
        recording.clear();
        releasePreview();
        start.disabled = false;
        stop.disabled = true;
        clear.disabled = true;
        status.textContent = '仅在手动开启后记录；未保存之前的音频。';
    }

    start.addEventListener('click', () => {
        if (!isMicrophoneActive()) {
            status.textContent = '请先连接并开启聊天麦克风，再保存录音。';
            return;
        }
        releasePreview();
        recording.start();
        capturing = true;
        start.disabled = true;
        stop.disabled = false;
        clear.disabled = false;
        status.textContent = '正在保存接下来最多 10 秒的发送音频，请自然说话。';
        timer = window.setTimeout(finish, 10000);
    });
    stop.addEventListener('click', finish);
    clear.addEventListener('click', reset);
    player.addEventListener('play', async () => {
        if (resumingPlayback) return;
        // 暂停到麦克风确实关闭，避免试听声音再次进入 ASR 和声纹匹配。
        player.pause();
        const preview = url;
        try {
            await beforePlayback();
            if (!preview || preview !== url) return;
            resumingPlayback = true;
            await player.play();
        } catch {
            status.textContent = '无法开始试听，请关闭麦克风后重试或下载 WAV。';
        } finally {
            resumingPlayback = false;
        }
    });
    window.addEventListener('pagehide', reset);
    window.addEventListener('newtalk:members-changed', reset);

    return {
        push(buffer) {
            if (!capturing) return;
            recording.push(buffer);
            if (!recording.active) finish();
        },
        finish,
    };
}
