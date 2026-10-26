import {initializeIdentity, selectedTextSpeaker} from './identity.js?v=20261024.1';
import {initializeMemoryCenter} from './memory-center.js?v=20261019.3';
import {initializeCustomSelects} from './custom-select.js?v=20261019.2';
import {initializeContextDashboard} from './context-dashboard.js?v=20261025.1';
import {initializeAudioDiagnostics} from './audio-diagnostics.js?v=20261023.2';
import {captureOriginMs, outputPerformanceTime} from './voice-latency.js?v=20261024.1';
import {createDeviceControls} from './device-control.js?v=20261025.1';
import {initializeReminders} from './reminders.js?v=20261026.1';
import {initializeMusic} from './music.js?v=20261026.3';

const statusElement = document.querySelector('#connectionStatus');
const statusLabel = document.querySelector('#statusLabel');
const endpointElement = document.querySelector('#endpoint');
const connectionButton = document.querySelector('#connectionButton');
const deviceControlStatus = document.querySelector('#deviceControlStatus');
const pingButton = document.querySelector('#pingButton');
const clearDialogueButton = document.querySelector('#clearDialogueButton');
const eventLog = document.querySelector('#eventLog');
const chatForm = document.querySelector('#chatForm');
const messageInput = document.querySelector('#messageInput');
const messageList = document.querySelector('#messageList');
const sendButton = document.querySelector('#sendButton');
const audioStatus = document.querySelector('#audioStatus');
const stopAudioButton = document.querySelector('#stopAudioButton');
const micStatus = document.querySelector('#micStatus');
const micButton = document.querySelector('#micButton');
const voiceProcessingStatus = document.querySelector('#voiceProcessingStatus');
const voiceLevelMeter = document.querySelector('#voiceLevelMeter');
const voiceLevelBar = document.querySelector('#voiceLevelBar');
const voiceLevelText = document.querySelector('#voiceLevelText');
const voiceVadStatus = document.querySelector('#voiceVadStatus');
const voiceRecognitionStatus = document.querySelector('#voiceRecognitionStatus');
const greetingTitle = document.querySelector('#greetingTitle');
const navigationItems = document.querySelectorAll('.nav-item');
const viewTriggers = document.querySelectorAll('[data-view-target]');
const viewSections = document.querySelectorAll('.view-section');

function showView(viewName, {updateHash = true, moveFocus = true} = {}) {
    const target = document.querySelector(`[data-view="${viewName}"]`);
    if (!target) return;

    for (const section of viewSections) {
        const isActive = section === target;
        section.hidden = !isActive;
        section.classList.toggle('is-active', isActive);
    }
    for (const item of navigationItems) {
        const isCurrent = item.dataset.viewTarget === viewName;
        item.classList.toggle('active', isCurrent);
        if (isCurrent) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
    }

    if (updateHash) {
        const hash = viewName === 'memory' ? '#memoryCenter' : `#${viewName}`;
        history.replaceState(null, '', hash);
    }
    window.scrollTo({top: 0, behavior: 'smooth'});
    window.dispatchEvent(new CustomEvent('newtalk:view-changed', {detail: {view: viewName}}));
    if (!moveFocus) return;
    if (viewName === 'conversation') {
        requestAnimationFrame(() => messageInput.focus());
        return;
    }
    const heading = target.querySelector('h1, h2, h3');
    if (heading) {
        heading.tabIndex = -1;
        requestAnimationFrame(() => heading.focus());
    }
}

function initializeHomeShell() {
    const hour = new Date().getHours();
    const greeting = hour < 6
        ? '夜深了。'
        : hour < 12
            ? '早上好。'
            : hour < 18
                ? '下午好。'
                : '晚上好。';

    greetingTitle.textContent = greeting;
    for (const trigger of viewTriggers) {
        trigger.addEventListener('click', (event) => {
            event.preventDefault();
            showView(trigger.dataset.viewTarget);
        });
    }

    const initialView = location.hash === '#tools' ? 'tools' : location.hash === '#context'
        ? 'context'
        : location.hash === '#conversation'
        ? 'conversation'
        : location.hash === '#family'
            ? 'family'
            : location.hash === '#memoryCenter'
                ? 'memory'
                : 'home';
    showView(initialView, {updateHash: false, moveFocus: false});
}

let socket = null;
let deviceControls = null;
let reminders = null;
let music = null;
let eventSequence = 0;
let advertisedAudioFormat = null;
let advertisedInputFormat = null;
const pendingEvents = new Set();
let pendingDialogueClear = null;
const messagesByTurn = new Map();
const turnStartedAt = new Map();
const voiceMessagesByUtterance = new Map();
const turnTelemetry = new Map();
const voiceTimings = new Map();

const TURN_METRIC_LABELS = [
    ['speechEndToAudibleMs', '说完→非静音输出（VAD 末尾估算）'],
    ['vadToAudibleMs', '收到判停→非静音输出'],
    ['speechMs', 'VAD 语音段（含尾静音）'],
    ['asrMs', '收到判停→ASR Final'],
    ['speakerMs', '声纹 Provider（与 ASR 有并行）'],
    ['llmFirstMs', 'Turn→文本首字'],
    ['ttsFirstMs', 'Turn→收到 TTS 首帧'],
    ['playbackMs', 'Turn→PCM 开始渲染'],
    ['audibleMs', 'Turn→非静音输出'],
];

function formatDuration(milliseconds) {
    const value = Number(milliseconds);
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value < 1000) return `${Math.round(value)} ms`;
    return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`;
}

function renderTurnMetrics(turnId) {
    const message = messagesByTurn.get(turnId);
    const telemetry = turnTelemetry.get(turnId);
    const details = message?.querySelector('.turn-metrics');
    const list = details?.querySelector('dl');
    if (!details || !list || !telemetry) return;

    const available = TURN_METRIC_LABELS.filter(([key]) => Number.isFinite(telemetry[key]));
    details.hidden = available.length === 0;
    list.replaceChildren();
    for (const [key, label] of available) {
        const term = document.createElement('dt');
        const description = document.createElement('dd');
        term.textContent = label;
        description.textContent = formatDuration(telemetry[key]);
        list.append(term, description);
    }
}

function recordTurnMetric(turnId, key, value) {
    const telemetry = turnTelemetry.get(turnId);
    const numericValue = Number(value);
    if (!telemetry || telemetry[key] !== undefined || !Number.isFinite(numericValue) || numericValue < 0) return;
    telemetry[key] = numericValue;
    renderTurnMetrics(turnId);
}

function ensureVoiceTiming(utteranceId) {
    if (!voiceTimings.has(utteranceId)) voiceTimings.set(utteranceId, {});
    return voiceTimings.get(utteranceId);
}

function updateVoiceProcessing(settings = null) {
    if (!settings) {
        voiceProcessingStatus.textContent = '麦克风未开启';
        return;
    }
    const describe = (key, label) => settings[key] === true
        ? `${label} 开`
        : settings[key] === false
            ? `${label} 关`
            : `${label} 未知`;
    voiceProcessingStatus.textContent = [
        describe('echoCancellation', 'AEC'),
        describe('noiseSuppression', '降噪'),
        describe('autoGainControl', '增益'),
    ].join(' · ');
}

function updateVoiceLevel(buffer) {
    const samples = new Int16Array(buffer);
    if (!samples.length) return;
    let squareSum = 0;
    for (const sample of samples) {
        const normalized = sample / 32768;
        squareSum += normalized * normalized;
    }
    const rms = Math.sqrt(squareSum / samples.length);
    const decibels = rms > 0 ? 20 * Math.log10(rms) : -60;
    const percentage = Math.max(0, Math.min(100, ((decibels + 60) / 60) * 100));
    voiceLevelBar.style.width = `${percentage.toFixed(1)}%`;
    voiceLevelMeter.setAttribute('aria-valuenow', String(Math.round(percentage)));
    voiceLevelText.textContent = rms > 0.0001 ? `${Math.round(decibels)} dBFS` : '静音';
}

function resetVoiceActivityDiagnostics() {
    voiceLevelBar.style.width = '0%';
    voiceLevelMeter.setAttribute('aria-valuenow', '0');
    voiceLevelText.textContent = '待命';
    voiceVadStatus.textContent = '等待麦克风';
}

class PcmPlayer {
    constructor() {
        this.context = null;
        this.node = null;
        this.format = null;
        this.active = null;
        this.queuedBytes = 0;
        this.playRequested = false;
    }

    async prepare(format) {
        if (!format) throw new Error('服务器没有提供音频格式');
        if (format.codec !== 'pcm_s16le' || format.channels !== 1) {
            throw new Error(`不支持的音频格式：${format.codec}/${format.channels}ch`);
        }
        if (!this.context) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass || !window.AudioWorkletNode) {
                throw new Error('当前浏览器不支持 AudioWorklet');
            }
            this.context = new AudioContextClass({
                latencyHint: 'interactive',
                sampleRate: format.sample_rate,
            });
            await this.context.audioWorklet.addModule('/pcm-player-worklet.js?v=20261024.1');
            if (this.context.sampleRate !== format.sample_rate) {
                throw new Error(`浏览器采样率为 ${this.context.sampleRate}，无法播放 ${format.sample_rate}Hz PCM`);
            }
            this.node = new AudioWorkletNode(this.context, 'newtalk-pcm-player');
            this.node.connect(this.context.destination);
            this.node.port.addEventListener('message', (event) => this.handleWorkletEvent(event.data));
            this.node.port.start();
        }
        this.format = format;
        if (this.context.state === 'suspended') await this.context.resume();
    }

    begin(metadata) {
        if (!this.node || !this.format) {
            this.setStatus('error', '音频未初始化');
            return;
        }
        if (
            metadata.codec !== this.format.codec
            || metadata.sample_rate !== this.format.sample_rate
            || metadata.channels !== this.format.channels
        ) {
            this.setStatus('error', '音频格式发生变化');
            return;
        }
        this.node.port.postMessage({type: 'reset', stream_id: metadata.stream_id});
        this.active = {
            streamId: metadata.stream_id,
            turnId: metadata.turn_id,
            stopped: false,
            firstFrameSeen: false,
        };
        this.queuedBytes = 0;
        this.playRequested = false;
        this.setStatus('buffering', '音频缓冲中');
        stopAudioButton.disabled = false;
    }

    push(buffer) {
        if (!this.active || this.active.stopped || !this.node) return;
        if (buffer.byteLength % 2 !== 0) {
            this.stop('PCM 帧长度错误');
            return;
        }
        if (!this.active.firstFrameSeen) {
            this.active.firstFrameSeen = true;
            const telemetry = turnTelemetry.get(this.active.turnId);
            if (telemetry) telemetry.firstFrameAt = performance.now();
            const startedAt = turnStartedAt.get(this.active.turnId);
            if (startedAt) recordTurnMetric(this.active.turnId, 'ttsFirstMs', performance.now() - startedAt);
        }
        this.queuedBytes += buffer.byteLength;
        this.node.port.postMessage({type: 'audio', buffer}, [buffer]);

        const prebufferBytes = this.format.sample_rate * 2 * 0.06;
        if (!this.playRequested && this.queuedBytes >= prebufferBytes) {
            this.playRequested = true;
            this.node.port.postMessage({type: 'play'});
        }
    }

    end(metadata) {
        if (!this.active || metadata.stream_id !== this.active.streamId || !this.node) return;
        if (this.active.stopped) {
            this.active = null;
            return;
        }
        if (!this.playRequested) {
            this.playRequested = true;
            this.node.port.postMessage({type: 'play'});
        }
        this.node.port.postMessage({type: 'end'});
        this.setStatus('playing', '播放收尾中');
    }

    fail() {
        if (this.active) turnStartedAt.delete(this.active.turnId);
        this.node?.port.postMessage({type: 'reset'});
        this.active = null;
        stopAudioButton.disabled = true;
        this.setStatus('error', '语音生成失败');
    }

    stop(label = '播放已停止') {
        if (!this.active) return;
        this.active.stopped = true;
        this.node?.port.postMessage({type: 'reset'});
        stopAudioButton.disabled = true;
        this.setStatus('idle', label);
    }

    reset() {
        this.node?.port.postMessage({type: 'reset'});
        this.active = null;
        stopAudioButton.disabled = true;
        this.setStatus('idle', '音频待命');
    }

    handleWorkletEvent(event) {
        if (!this.active) return;
        // 旧 Worklet 回执可能晚于新流的 reset；不能把旧流的声音计到下一轮。
        if (event.stream_id !== this.active.streamId || this.active.stopped) return;
        if (event.type === 'playback_audible') {
            const telemetry = turnTelemetry.get(this.active.turnId);
            if (!telemetry || telemetry.audibleReported) return;
            telemetry.audibleReported = true;
            const output = outputPerformanceTime(this.context, event.context_time, performance.now());
            const metrics = {};
            for (const [name, at, key] of [
                ['turn_to_audible_ms', telemetry.startedAt, 'audibleMs'],
                ['speech_end_to_audible_ms', telemetry.lastVoiceAt, 'speechEndToAudibleMs'],
                ['vad_to_audible_ms', telemetry.speechEndedAt, 'vadToAudibleMs'],
                ['tts_to_audible_ms', telemetry.firstFrameAt, null],
            ]) {
                const elapsed = output.atMs - at;
                if (!Number.isFinite(at) || !Number.isFinite(elapsed) || elapsed < 0) continue;
                metrics[name] = Number(elapsed.toFixed(1));
                if (key) recordTurnMetric(this.active.turnId, key, metrics[name]);
            }
            const metric = {type: 'playback_audible', event_id: nextEventId('audible'),
                turn_id: this.active.turnId, stream_id: this.active.streamId,
                output_clock: output.clock, metrics};
            if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(metric));
            appendProtocolEvent('outgoing', metric);
            return;
        }
        if (event.type === 'playback_started') {
            this.setStatus('playing', '正在播放');
            const startedAt = turnStartedAt.get(this.active.turnId);
            const elapsedMs = startedAt ? performance.now() - startedAt : 0;
            if (startedAt) recordTurnMetric(this.active.turnId, 'playbackMs', elapsedMs);
            turnStartedAt.delete(this.active.turnId);
            const metric = {
                type: 'playback_started',
                event_id: nextEventId('playback'),
                turn_id: this.active.turnId,
                stream_id: this.active.streamId,
                elapsed_ms: Number(elapsedMs.toFixed(1)),
            };
            if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(metric));
            appendProtocolEvent('outgoing', metric);
            return;
        }
        if (event.type === 'playback_drained') {
            const completed = this.active;
            this.setStatus('idle', '播放完成');
            stopAudioButton.disabled = true;
            this.active = null;
            const metric = {
                type: 'playback_drained',
                event_id: nextEventId('playback'),
                turn_id: completed.turnId,
                stream_id: completed.streamId,
            };
            if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(metric));
            appendProtocolEvent('outgoing', metric);
        }
    }

    setStatus(state, label) {
        audioStatus.dataset.state = state;
        audioStatus.textContent = label;
        reminders?.setBusy('chat-audio', state === 'buffering' || state === 'playing');
        music?.setBusy('chat-audio', state === 'buffering' || state === 'playing');
    }
}

const audioPlayer = new PcmPlayer();

class MicrophoneRecorder {
    constructor() {
        this.context = null;
        this.stream = null;
        this.source = null;
        this.node = null;
        this.captureId = null;
        this.active = false;
        this.lastLevelUpdate = 0;
        this.captureOriginMs = null;
    }

    async start(format) {
        if (this.active) return;
        if (!format || format.codec !== 'pcm_s16le' || format.sample_rate !== 16000 || format.channels !== 1) {
            throw new Error('服务器没有提供受支持的麦克风格式');
        }
        if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
            throw new Error('当前浏览器不支持麦克风 AudioWorklet');
        }

        this.stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
        const audioTrack = this.stream.getAudioTracks()[0];
        updateVoiceProcessing(audioTrack?.getSettings?.() ?? {});
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        this.context = new AudioContextClass({latencyHint: 'interactive'});
        await this.context.audioWorklet.addModule('/mic-recorder-worklet.js?v=20261024.1');
        this.source = this.context.createMediaStreamSource(this.stream);
        this.node = new AudioWorkletNode(this.context, 'newtalk-mic-recorder');
        const mute = this.context.createGain();
        mute.gain.value = 0;
        this.source.connect(this.node);
        this.node.connect(mute).connect(this.context.destination);
        this.captureId = nextEventId('capture');
        this.captureOriginMs = null;
        this.active = true;
        this.node.port.onmessage = (event) => {
            if (!this.active || event.data?.type !== 'audio_frame') return;
            const now = performance.now();
            this.captureOriginMs = captureOriginMs(event.data, this.context, now);
            if (now - this.lastLevelUpdate >= 120) {
                this.lastLevelUpdate = now;
                updateVoiceLevel(event.data.buffer);
            }
            if (socket?.readyState === WebSocket.OPEN) {
                socket.send(event.data.buffer);
                audioDiagnostics.push(event.data.buffer);
            }
        };
        const payload = {
            type: 'audio_input_start',
            event_id: nextEventId('audio-start'),
            capture_id: this.captureId,
            format,
        };
        socket.send(JSON.stringify(payload));
        appendProtocolEvent('outgoing', payload);
        this.setStatus('listening', `监听中 ${this.context.sampleRate / 1000}k→16k`);
        voiceVadStatus.textContent = '等待说话';
        voiceRecognitionStatus.textContent = '等待语音';
        micButton.textContent = '关闭麦克风';
    }

    async stop(sendEvent = true) {
        if (!this.active && !this.stream) return;
        const captureId = this.captureId;
        const context = this.context;
        this.active = false;
        audioDiagnostics.finish();
        this.node?.disconnect();
        this.source?.disconnect();
        this.stream?.getTracks().forEach((track) => track.stop());
        this.context = null;
        this.stream = null;
        this.source = null;
        this.node = null;
        this.captureId = null;
        this.lastLevelUpdate = 0;
        this.captureOriginMs = null;
        if (sendEvent && captureId && socket?.readyState === WebSocket.OPEN) {
            const payload = {
                type: 'audio_input_stop',
                event_id: nextEventId('audio-stop'),
                capture_id: captureId,
            };
            socket.send(JSON.stringify(payload));
            appendProtocolEvent('outgoing', payload);
        }
        this.setStatus('idle', '麦克风待命');
        updateVoiceProcessing();
        resetVoiceActivityDiagnostics();
        micButton.textContent = '开启麦克风';
        // 先摘除本轮资源，旧 AudioContext 迟到关闭不能清掉新一轮采集。
        await context?.close();
    }

    setStatus(state, label) {
        micStatus.dataset.state = state;
        micStatus.textContent = label;
    }
}

const microphone = new MicrophoneRecorder();
const audioDiagnostics = initializeAudioDiagnostics({
    isMicrophoneActive: () => microphone.active && socket?.readyState === WebSocket.OPEN,
    beforePlayback: async () => {
        audioPlayer.stop();
        await microphone.stop();
    },
});

function websocketUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${window.location.host}/ws`;
}

function nextEventId(prefix) {
    return `${prefix}-${Date.now()}-${++eventSequence}`;
}

function updateComposer() {
    const connected = socket?.readyState === WebSocket.OPEN;
    sendButton.disabled = !connected || !messageInput.value.trim();
    micButton.disabled = !connected;
    clearDialogueButton.disabled = !connected || pendingDialogueClear !== null;
}

function setStatus(state, label) {
    statusElement.dataset.state = state;
    statusLabel.textContent = label;
    const connected = state === 'online';
    pingButton.disabled = !connected;
    connectionButton.disabled = state === 'connecting' || state === 'closing';
    connectionButton.textContent = connected ? '断开' : state === 'closing' ? '断开中' : '连接';
    updateComposer();
}

function appendProtocolEvent(direction, payload) {
    const item = document.createElement('li');
    const badge = document.createElement('span');
    const content = document.createElement('pre');

    badge.className = `event-direction ${direction}`;
    badge.textContent = direction === 'incoming' ? 'IN' : direction === 'outgoing' ? 'OUT' : 'SYS';
    content.textContent = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
    item.append(badge, content);
    eventLog.prepend(item);
}

function appendMessage(role, text, meta, lane = null) {
    const item = document.createElement('li');
    const heading = document.createElement('div');
    const roleLabel = document.createElement('span');
    const stateLabel = document.createElement('span');
    const metaLabel = document.createElement('code');
    const content = document.createElement('p');

    item.className = `message ${role}`;
    if (lane) item.dataset.lane = lane;
    heading.className = 'message-heading';
    roleLabel.textContent = role === 'user' ? '你' : 'Newtalk';
    roleLabel.className = 'message-role';
    stateLabel.className = 'message-state';
    stateLabel.hidden = true;
    metaLabel.textContent = meta || '';
    content.className = 'message-content';
    content.textContent = text;
    heading.append(roleLabel, stateLabel, metaLabel);
    item.append(heading, content);
    if (role === 'assistant') {
        const contextButton = document.createElement('button');
        contextButton.type = 'button';
        contextButton.className = 'context-open-button';
        contextButton.textContent = '查看上下文';
        heading.append(contextButton);
        const metrics = document.createElement('details');
        const summary = document.createElement('summary');
        const list = document.createElement('dl');
        metrics.className = 'turn-metrics';
        metrics.hidden = true;
        summary.textContent = '本轮耗时';
        summary.title = '浏览器根据本轮事件计算的观测值';
        metrics.append(summary, list);
        item.append(metrics);
    }
    messageList.append(item);
    item.scrollIntoView({block: 'nearest'});
    return item;
}

function setMessageState(message, state, label = '') {
    if (!message) return;
    message.dataset.state = state;
    const stateLabel = message.querySelector('.message-state');
    if (!stateLabel) return;
    stateLabel.textContent = label;
    stateLabel.hidden = !label;
}

function markStreamingMessagesInterrupted() {
    for (const item of messagesByTurn.values()) {
        if (item.dataset.state === 'streaming') {
            setMessageState(item, 'failed', '连接中断');
            const content = item.querySelector('.message-content');
            if (!content.textContent) content.textContent = '连接中断，回复未完成。';
        }
    }
}

function handleIncoming(payload) {
    appendProtocolEvent('incoming', payload);
    if (payload.type.startsWith('reminder')) {
        reminders?.handle(payload);
        return;
    }

    if (payload.type === 'media_control' || payload.type === 'media_error' || payload.type === 'media_status') {
        if (payload.type === 'media_control' && payload.turn_id) {
            reminders?.setBusy('generation', false);
            reminders?.setBusy('user', false);
            music?.setBusy('generation', false);
            music?.setBusy('user', false);
            const bubble = messagesByTurn.get(payload.turn_id);
            if (bubble && !bubble.querySelector('.message-content')?.textContent.trim()) {
                bubble.remove(); messagesByTurn.delete(payload.turn_id);
            }
        }
        music?.handle(payload);
        return;
    }

    if (payload.type === 'device_control') {
        reminders?.setBusy('generation', false);
        music?.setBusy('generation', false);
        reminders?.setBusy('user', false);
        music?.setBusy('user', false);
        pendingEvents.delete(payload.event_id);
        const message = messagesByTurn.get(payload.turn_id);
        if (message && !message.querySelector('.message-content')?.textContent.trim()) {
            message.remove();
            messagesByTurn.delete(payload.turn_id);
        }
        if (payload.event_id?.startsWith('voice-')) {
            voiceMessagesByUtterance.delete(payload.event_id.slice(6));
            voiceRecognitionStatus.textContent = '控制指令已识别';
        }
        void Promise.resolve(deviceControls?.handle(payload)).catch(error => {
            console.warn('Device control failed', error);
        });
        return;
    }
    if (payload.type === 'device_control_status') {
        deviceControls?.status(payload);
        return;
    }

    if (payload.type === 'hello') {
        music?.capabilities(payload.music?.enabled, payload.podcasts?.enabled);
        advertisedInputFormat = payload.audio.input;
        advertisedAudioFormat = payload.audio.output;
        audioStatus.textContent = `${advertisedAudioFormat.sample_rate / 1000}k PCM 待命`;
        renderDialogueSnapshot(payload.dialogue?.items || []);
        return;
    }

    if (payload.type === 'audio_input_ready') {
        microphone.setStatus('listening', '麦克风监听中');
        voiceVadStatus.textContent = '等待说话';
        voiceRecognitionStatus.textContent = '等待语音';
        return;
    }

    if (payload.type === 'vad_speech_start') {
        reminders?.setBusy('user', true);
        if (!payload.media_only) music?.setBusy('user', true);
        const timing = ensureVoiceTiming(payload.utterance_id);
        timing.speechStartedAt = performance.now();
        timing.audioStartedMs = payload.audio_ms;
        if (!payload.media_only) audioPlayer.stop('检测到说话，已停止播放');
        microphone.setStatus('speech', '检测到说话');
        voiceVadStatus.textContent = '正在说话';
        voiceRecognitionStatus.textContent = payload.media_only ? '播放中，仅监听暂停／停止指令' : '录音中';
        return;
    }

    if (payload.type === 'vad_speech_end') {
        const timing = ensureVoiceTiming(payload.utterance_id);
        timing.speechEndedAt = performance.now();
        if (Number.isFinite(payload.audio_ms) && Number.isFinite(timing.audioStartedMs)) {
            timing.speechMs = payload.audio_ms - timing.audioStartedMs;
        }
        if (payload.capture_id === microphone.captureId && Number.isFinite(microphone.captureOriginMs)
            && Number.isFinite(payload.last_voice_audio_ms)) {
            timing.lastVoiceAt = microphone.captureOriginMs + payload.last_voice_audio_ms;
        }
        microphone.setStatus('listening', '识别处理中');
        voiceVadStatus.textContent = `VAD 语音段 ${formatDuration(timing.speechMs)}（含尾静音）`;
        voiceRecognitionStatus.textContent = '识别中';
        return;
    }

    if (payload.type === 'asr_partial') {
        microphone.setStatus('speech', `识别中：${payload.text}`);
        return;
    }

    if (payload.type === 'asr_final') {
        if (payload.disposition === 'ignored_media' || payload.disposition === 'media_control') {
            reminders?.setBusy('user', false);
            music?.setBusy('user', false);
            voiceTimings.delete(payload.utterance_id);
            if (payload.disposition === 'media_control' && payload.text) {
                const message = appendMessage('user', payload.text, payload.utterance_id.slice(0, 8), 'guest');
                message.querySelector('.message-role').textContent = '语音控制';
            }
            microphone.setStatus('listening', payload.disposition === 'media_control' ? '媒体控制已识别' : '播放中');
            voiceRecognitionStatus.textContent = payload.disposition === 'media_control'
                ? '播放控制已识别，正在执行' : '未触发播放控制';
            return;
        }
        if (!payload.text) reminders?.setBusy('user', false);
        if (!payload.text) music?.setBusy('user', false);
        const timing = ensureVoiceTiming(payload.utterance_id);
        if (timing.speechEndedAt) timing.asrMs = performance.now() - timing.speechEndedAt;
        if (payload.text) {
            const message = appendMessage('user', payload.text, payload.utterance_id.slice(0, 8), 'guest');
            voiceMessagesByUtterance.set(payload.utterance_id, message);
        }
        microphone.setStatus('listening', '正在确认说话人');
        voiceRecognitionStatus.textContent = payload.text ? '文本已识别 · 正在确认说话人' : '未识别到有效文字';
        return;
    }

    if (payload.type === 'speaker_resolved') {
        const timing = ensureVoiceTiming(payload.utterance_id);
        const speakerElapsed = Number(payload.provider_elapsed_ms ?? payload.elapsed_ms);
        if (Number.isFinite(speakerElapsed)) timing.speakerMs = speakerElapsed;
        const message = voiceMessagesByUtterance.get(payload.utterance_id);
        if (message) {
            message.querySelector('.message-role').textContent = payload.matched
                ? payload.display_name
                : '访客';
            message.dataset.lane = payload.matched ? 'family' : 'guest';
            voiceMessagesByUtterance.delete(payload.utterance_id);
        }
        microphone.setStatus(
            'listening',
            payload.matched ? `已识别：${payload.display_name}` : '未匹配成员，按 Guest 对话',
        );
        voiceRecognitionStatus.textContent = payload.matched
            ? `文本已识别 · ${payload.display_name}`
            : '文本已识别 · 访客';
        return;
    }

    if (payload.type === 'asr_failed') {
        reminders?.setBusy('user', false);
        music?.setBusy('user', false);
        microphone.setStatus('error', '语音识别失败，请查看服务日志');
        voiceRecognitionStatus.textContent = '识别失败';
        return;
    }

    if (payload.type === 'turn_cancelled') {
        reminders?.setBusy('generation', false);
        music?.setBusy('generation', false);
        stopAudioButton.disabled = true;
        const message = messagesByTurn.get(payload.turn_id);
        setMessageState(message, 'interrupted', '已中断');
        const content = message?.querySelector('.message-content');
        if (content && !content.textContent.trim()) content.textContent = '回复已被新输入中断。';
        return;
    }

    if (payload.type === 'audio_stop') {
        audioPlayer.stop('已被新语音打断');
        return;
    }

    if (payload.type === 'turn_waiting') {
        reminders?.setBusy('generation', false);
        music?.setBusy('generation', false);
        reminders?.setBusy('user', false);
        music?.setBusy('user', false);
        setMessageState(messagesByTurn.get(payload.turn_id), 'waiting', '提醒播报后回复');
        return;
    }

    if (payload.type === 'turn_started') {
        reminders?.setBusy('generation', true);
        music?.setBusy('generation', true);
        reminders?.setBusy('user', false);
        music?.setBusy('user', false);
        stopAudioButton.disabled = false;
        pendingEvents.delete(payload.event_id);
        const startedAt = performance.now();
        turnStartedAt.set(payload.turn_id, startedAt);
        const telemetry = {startedAt};
        if (typeof payload.event_id === 'string' && payload.event_id.startsWith('voice-')) {
            const utteranceId = payload.event_id.slice('voice-'.length);
            const voiceTiming = voiceTimings.get(utteranceId);
            if (voiceTiming) {
                for (const key of ['speechMs', 'asrMs', 'speakerMs', 'speechEndedAt', 'lastVoiceAt']) {
                    if (Number.isFinite(voiceTiming[key])) telemetry[key] = voiceTiming[key];
                }
                voiceTimings.delete(utteranceId);
            }
        }
        turnTelemetry.set(payload.turn_id, telemetry);
        const lane = payload.speaker?.guest === false ? 'family' : 'guest';
        const message = appendMessage('assistant', '', payload.turn_id.slice(0, 8), lane);
        message.dataset.turnId = payload.turn_id;
        setMessageState(message, 'streaming', '正在生成');
        messagesByTurn.set(payload.turn_id, message);
        renderTurnMetrics(payload.turn_id);
        return;
    }

    if (payload.type === 'text_delta') {
        const message = messagesByTurn.get(payload.turn_id);
        if (message) {
            const startedAt = turnStartedAt.get(payload.turn_id);
            if (startedAt) recordTurnMetric(payload.turn_id, 'llmFirstMs', performance.now() - startedAt);
            message.querySelector('.message-content').textContent += payload.delta;
            message.scrollIntoView({block: 'nearest'});
        }
        return;
    }

    if (payload.type === 'audio_start') {
        setMessageState(messagesByTurn.get(payload.turn_id), 'speaking', '正在播报');
        audioPlayer.begin(payload);
        return;
    }

    if (payload.type === 'audio_end') {
        setMessageState(messagesByTurn.get(payload.turn_id), 'completed');
        audioPlayer.end(payload);
        return;
    }

    if (payload.type === 'audio_failed') {
        setMessageState(messagesByTurn.get(payload.turn_id), 'failed', '播报失败');
        audioPlayer.fail();
        return;
    }

    if (payload.type === 'turn_completed') {
        reminders?.setBusy('generation', false);
        music?.setBusy('generation', false);
        if (!audioPlayer.active || audioPlayer.active.stopped) stopAudioButton.disabled = true;
        const message = messagesByTurn.get(payload.turn_id);
        if (message) {
            message.querySelector('.message-content').textContent = payload.text;
            if (message.dataset.state !== 'speaking') setMessageState(message, 'completed');
        }
        return;
    }

    if (payload.type === 'turn_failed') {
        reminders?.setBusy('generation', false);
        music?.setBusy('generation', false);
        stopAudioButton.disabled = true;
        pendingEvents.delete(payload.event_id);
        const message = messagesByTurn.get(payload.turn_id);
        if (message) {
            const overBudget = payload.code === 'context_budget_exceeded';
            message.querySelector('.message-content').textContent = overBudget
                ? '本次模型请求超出输入预算，未发送。请缩短输入或调整预算；用户输入仍保留在短期记录中。'
                : '回复生成失败，请重新发送。';
            setMessageState(message, 'failed', overBudget ? '预算超限' : '生成失败');
        }
        audioPlayer.fail();
        return;
    }

    if (payload.type === 'dialogue_cleared') {
        if (payload.event_id === pendingDialogueClear) pendingDialogueClear = null;
        messageList.replaceChildren();
        messagesByTurn.clear();
        turnStartedAt.clear();
        voiceMessagesByUtterance.clear();
        turnTelemetry.clear();
        voiceTimings.clear();
        updateComposer();
        appendProtocolEvent('system', '本轮短期对话已清除，画像和长期记忆未受影响');
        return;
    }

    if (payload.type === 'error') {
        if (payload.event_id) pendingEvents.delete(payload.event_id);
        if (payload.event_id === pendingDialogueClear) {
            pendingDialogueClear = null;
            updateComposer();
        }
        appendMessage('assistant', `消息未处理：${payload.message}`, payload.code);
    }
}

function renderDialogueSnapshot(items) {
    messageList.replaceChildren();
    messagesByTurn.clear();
    turnStartedAt.clear();
    turnTelemetry.clear();
    voiceTimings.clear();
    if (!items.length) return;
    for (const item of items) {
        const user = appendMessage('user', item.user_text, item.turn_id.slice(0, 8), item.lane);
        user.querySelector('.message-role').textContent = item.speaker.display_name;
        if (!item.assistant_text) {
            user.title = '用户输入已保留；本轮没有已完成的助手回答。';
            continue;
        }
        const assistant = appendMessage('assistant', item.assistant_text, item.turn_id.slice(0, 8), item.lane);
        assistant.dataset.turnId = item.turn_id;
        setMessageState(assistant, 'completed');
        messagesByTurn.set(item.turn_id, assistant);
    }
}

function disconnect() {
    return deviceControls?.execute('disconnect');
}

function closeConnection(connection) {
    if (socket !== connection) return;
    reminders?.connect(null);
    music?.connect(null);
    if (socket?.readyState === WebSocket.OPEN) {
        const event = {type: 'close', event_id: nextEventId('close')};
        socket.send(JSON.stringify(event));
        appendProtocolEvent('outgoing', event);
        setStatus('closing', '断开中');
        connection.close(1000, 'client requested close');
    }
}

function connect() {
    if (window.location.protocol === 'file:') {
        appendProtocolEvent('system', '请通过 HTTP 启动页面，不要使用 file:// 打开 index.html');
        setStatus('error', '页面地址错误');
        return;
    }

    if (socket) {
        if (socket.readyState === WebSocket.OPEN) disconnect();
        return;
    }

    setStatus('connecting', '连接中');
    socket = new WebSocket(websocketUrl());
    socket.binaryType = 'arraybuffer';
    const connection = socket;
    const controls = createDeviceControls({
        isCurrent: () => socket === connection && connection.readyState === WebSocket.OPEN,
        stopReply: notify => {
            audioPlayer.stop();
            if (notify && connection.readyState === WebSocket.OPEN) {
                const event = {type: 'device_control', action: 'stop_reply', event_id: nextEventId('control')};
                connection.send(JSON.stringify(event));
                appendProtocolEvent('outgoing', event);
            }
        },
        stopMicrophone: notify => {
            reminders?.setBusy('user', false);
            music?.setBusy('user', false);
            return microphone.stop(notify);
        },
        // 摄像头预览尚未接入，不能假称关闭了其他应用的摄像头。
        stopCamera: () => false,
        closeConnection: () => closeConnection(connection),
        sendResult: event => {
            connection.send(JSON.stringify(event));
            appendProtocolEvent('outgoing', event);
        },
        showStatus: message => {
            deviceControlStatus.hidden = false;
            deviceControlStatus.textContent = message;
        },
    });
    deviceControls = controls;

    socket.addEventListener('open', () => {
        if (socket !== connection) return;
        reminders?.connect(event => {
            if (socket === connection && connection.readyState === WebSocket.OPEN) connection.send(JSON.stringify(event));
        });
        music?.connect(event => {
            if (socket === connection && connection.readyState === WebSocket.OPEN) connection.send(JSON.stringify(event));
        });
        deviceControlStatus.hidden = true;
        deviceControlStatus.textContent = '';
        setStatus('online', '已连接');
        appendProtocolEvent('system', 'WebSocket connected');
        messageInput.focus();
    });

    socket.addEventListener('message', (event) => {
        if (socket !== connection) return;
        if (event.data instanceof ArrayBuffer) {
            appendProtocolEvent('incoming', {type: 'audio_frame', bytes: event.data.byteLength});
            audioPlayer.push(event.data);
            return;
        }
        let payload;
        try {
            payload = JSON.parse(event.data);
        } catch (error) {
            appendProtocolEvent('incoming', event.data);
            console.warn('WebSocket message is not valid JSON', error);
            return;
        }
        try {
            handleIncoming(payload);
        } catch (error) {
            const messageType = payload?.type || 'unknown';
            console.error('WebSocket message handler failed', {payload, error});
            appendProtocolEvent('system', `Message handling failed (${messageType})`);
        }
    });

    socket.addEventListener('close', async (event) => {
        controls.dispose();
        if (socket !== connection) return;
        reminders?.connect(null);
        music?.connect(null);
        appendProtocolEvent('system', `WebSocket closed (${event.code})`);
        markStreamingMessagesInterrupted();
        pendingEvents.clear();
        pendingDialogueClear = null;
        audioPlayer.reset();
        try {
            await microphone.stop(false);
        } catch (error) {
            console.warn('Microphone cleanup failed', error);
        } finally {
            if (socket === connection) {
                socket = null;
                deviceControls = null;
                setStatus('offline', '离线');
                deviceControlStatus.hidden = false;
                deviceControlStatus.textContent = '连接已断开';
            }
        }
    });

    socket.addEventListener('error', () => {
        appendProtocolEvent('system', 'WebSocket connection error');
        setStatus('error', '连接异常');
    });
}

chatForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = messageInput.value.trim();
    if (!text || socket?.readyState !== WebSocket.OPEN) return;
    music?.setBusy('user', true);

    try {
        await audioPlayer.prepare(advertisedAudioFormat);
    } catch (error) {
        appendProtocolEvent('system', `Audio initialization failed: ${error.message}`);
        audioPlayer.setStatus('error', '音频不可用');
    }

    const payload = {
        type: 'text_input',
        event_id: nextEventId('text'),
        text,
        identity_id: selectedTextSpeaker()?.identity_id ?? null,
    };
    pendingEvents.add(payload.event_id);
    const speaker = selectedTextSpeaker();
    const lane = speaker ? 'family' : 'guest';
    const userMessage = appendMessage('user', text, payload.event_id.split('-').slice(-2).join('-'), lane);
    userMessage.querySelector('.message-role').textContent = speaker?.display_name ?? 'GUEST';
    socket.send(JSON.stringify(payload));
    appendProtocolEvent('outgoing', payload);
    messageInput.value = '';
    updateComposer();
});

messageInput.addEventListener('input', updateComposer);
messageInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});

connectionButton.addEventListener('click', connect);
pingButton.addEventListener('click', () => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const event = {type: 'ping', event_id: nextEventId('ping')};
    socket.send(JSON.stringify(event));
    appendProtocolEvent('outgoing', event);
});
clearDialogueButton.addEventListener('click', () => {
    if (!socket || socket.readyState !== WebSocket.OPEN || pendingDialogueClear) return;
    const confirmed = window.confirm(
        '确定开始新对话吗？\n\n将清除本轮全部短期对话，但不会删除成员、声纹、画像或长期记忆。',
    );
    if (!confirmed) return;
    const event = {
        type: 'dialogue_clear',
        event_id: nextEventId('dialogue-clear'),
    };
    pendingDialogueClear = event.event_id;
    updateComposer();
    socket.send(JSON.stringify(event));
    appendProtocolEvent('outgoing', event);
});
stopAudioButton.addEventListener('click', () => deviceControls?.execute('stop_reply'));
window.addEventListener('newtalk:voiceprint-recording', async () => {
    music?.pause('声纹录入期间暂停音乐');
    reminders?.stop();
    audioPlayer.stop();
    await microphone.stop();
});
micButton.addEventListener('click', async () => {
    if (microphone.active) {
        await deviceControls?.execute('microphone_off');
        return;
    }
    try {
        await audioPlayer.prepare(advertisedAudioFormat);
        await microphone.start(advertisedInputFormat);
    } catch (error) {
        await microphone.stop(false);
        microphone.setStatus('error', '麦克风不可用');
        appendProtocolEvent('system', `Microphone initialization failed: ${error.message}`);
    }
});

endpointElement.textContent = window.location.protocol === 'file:' ? 'HTTP runtime required' : websocketUrl();
music = initializeMusic();
reminders = initializeReminders({onPlaybackActive: active => music?.setBusy('reminder', active)});
initializeContextDashboard(showView);
initializeHomeShell();
initializeCustomSelects();
initializeMemoryCenter();
initializeIdentity(() => connect());
