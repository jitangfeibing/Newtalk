const statusElement = document.querySelector('#connectionStatus');
const statusLabel = document.querySelector('#statusLabel');
const endpointElement = document.querySelector('#endpoint');
const connectionButton = document.querySelector('#connectionButton');
const pingButton = document.querySelector('#pingButton');
const eventLog = document.querySelector('#eventLog');
const chatForm = document.querySelector('#chatForm');
const messageInput = document.querySelector('#messageInput');
const messageList = document.querySelector('#messageList');
const sendButton = document.querySelector('#sendButton');
const audioStatus = document.querySelector('#audioStatus');
const stopAudioButton = document.querySelector('#stopAudioButton');
const micStatus = document.querySelector('#micStatus');
const micButton = document.querySelector('#micButton');

let socket = null;
let eventSequence = 0;
let advertisedAudioFormat = null;
let advertisedInputFormat = null;
const pendingEvents = new Set();
const messagesByTurn = new Map();
const turnStartedAt = new Map();

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
            await this.context.audioWorklet.addModule('/pcm-player-worklet.js');
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
        this.node.port.postMessage({type: 'reset'});
        this.active = {
            streamId: metadata.stream_id,
            turnId: metadata.turn_id,
            stopped: false,
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
        if (event.type === 'playback_started') {
            this.setStatus('playing', '正在播放');
            const startedAt = turnStartedAt.get(this.active.turnId);
            const elapsedMs = startedAt ? performance.now() - startedAt : 0;
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
            this.setStatus('idle', '播放完成');
            stopAudioButton.disabled = true;
            this.active = null;
        }
    }

    setStatus(state, label) {
        audioStatus.dataset.state = state;
        audioStatus.textContent = label;
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
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        this.context = new AudioContextClass({latencyHint: 'interactive'});
        await this.context.audioWorklet.addModule('/mic-recorder-worklet.js');
        this.source = this.context.createMediaStreamSource(this.stream);
        this.node = new AudioWorkletNode(this.context, 'newtalk-mic-recorder');
        const mute = this.context.createGain();
        mute.gain.value = 0;
        this.source.connect(this.node);
        this.node.connect(mute).connect(this.context.destination);
        this.captureId = nextEventId('capture');
        this.active = true;
        this.node.port.onmessage = (event) => {
            if (!this.active || event.data?.type !== 'audio_frame') return;
            if (socket?.readyState === WebSocket.OPEN) socket.send(event.data.buffer);
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
        micButton.textContent = '关闭麦克风';
    }

    async stop(sendEvent = true) {
        if (!this.active && !this.stream) return;
        const captureId = this.captureId;
        this.active = false;
        this.node?.disconnect();
        this.source?.disconnect();
        this.stream?.getTracks().forEach((track) => track.stop());
        await this.context?.close();
        this.context = null;
        this.stream = null;
        this.source = null;
        this.node = null;
        this.captureId = null;
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
        micButton.textContent = '开启麦克风';
    }

    setStatus(state, label) {
        micStatus.dataset.state = state;
        micStatus.textContent = label;
    }
}

const microphone = new MicrophoneRecorder();

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

function appendMessage(role, text, meta) {
    const item = document.createElement('li');
    const heading = document.createElement('div');
    const roleLabel = document.createElement('span');
    const metaLabel = document.createElement('code');
    const content = document.createElement('p');

    item.className = `message ${role}`;
    heading.className = 'message-heading';
    roleLabel.textContent = role === 'user' ? 'YOU' : 'NEWTALK';
    metaLabel.textContent = meta;
    content.className = 'message-content';
    content.textContent = text;
    heading.append(roleLabel, metaLabel);
    item.append(heading, content);
    messageList.append(item);
    item.scrollIntoView({block: 'nearest'});
    return item;
}

function markStreamingMessagesInterrupted() {
    for (const item of messagesByTurn.values()) {
        if (item.dataset.state === 'streaming') {
            item.dataset.state = 'failed';
            const content = item.querySelector('.message-content');
            if (!content.textContent) content.textContent = '连接中断，回复未完成。';
        }
    }
}

function handleIncoming(payload) {
    appendProtocolEvent('incoming', payload);

    if (payload.type === 'hello') {
        advertisedInputFormat = payload.audio.input;
        advertisedAudioFormat = payload.audio.output;
        audioStatus.textContent = `${advertisedAudioFormat.sample_rate / 1000}k PCM 待命`;
        return;
    }

    if (payload.type === 'audio_input_ready') {
        microphone.setStatus('listening', '麦克风监听中');
        return;
    }

    if (payload.type === 'vad_speech_start') {
        microphone.setStatus('speech', '检测到说话');
        return;
    }

    if (payload.type === 'vad_speech_end') {
        microphone.setStatus('listening', '识别处理中');
        return;
    }

    if (payload.type === 'asr_final') {
        if (payload.text) appendMessage('user', payload.text, payload.utterance_id.slice(0, 8));
        microphone.setStatus('listening', '麦克风监听中');
        return;
    }

    if (payload.type === 'turn_cancelled') {
        const message = messagesByTurn.get(payload.turn_id);
        if (message) message.dataset.state = 'failed';
        return;
    }

    if (payload.type === 'audio_stop') {
        audioPlayer.stop('已被新语音打断');
        return;
    }

    if (payload.type === 'turn_started') {
        pendingEvents.delete(payload.event_id);
        turnStartedAt.set(payload.turn_id, performance.now());
        const message = appendMessage('assistant', '', payload.turn_id.slice(0, 8));
        message.dataset.state = 'streaming';
        messagesByTurn.set(payload.turn_id, message);
        return;
    }

    if (payload.type === 'text_delta') {
        const message = messagesByTurn.get(payload.turn_id);
        if (message) {
            message.querySelector('.message-content').textContent += payload.delta;
            message.scrollIntoView({block: 'nearest'});
        }
        return;
    }

    if (payload.type === 'audio_start') {
        audioPlayer.begin(payload);
        return;
    }

    if (payload.type === 'audio_end') {
        audioPlayer.end(payload);
        return;
    }

    if (payload.type === 'audio_failed') {
        audioPlayer.fail();
        return;
    }

    if (payload.type === 'turn_completed') {
        const message = messagesByTurn.get(payload.turn_id);
        if (message) {
            message.querySelector('.message-content').textContent = payload.text;
            message.dataset.state = 'completed';
        }
        return;
    }

    if (payload.type === 'turn_failed') {
        pendingEvents.delete(payload.event_id);
        const message = messagesByTurn.get(payload.turn_id);
        if (message) {
            message.querySelector('.message-content').textContent = '回复生成失败，请重新发送。';
            message.dataset.state = 'failed';
        }
        audioPlayer.fail();
        return;
    }

    if (payload.type === 'error') {
        if (payload.event_id) pendingEvents.delete(payload.event_id);
        appendMessage('assistant', `消息未处理：${payload.message}`, payload.code);
    }
}

function disconnect() {
    if (socket?.readyState === WebSocket.OPEN) {
        const event = {type: 'close', event_id: nextEventId('close')};
        socket.send(JSON.stringify(event));
        appendProtocolEvent('outgoing', event);
        setStatus('closing', '断开中');
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

    socket.addEventListener('open', () => {
        setStatus('online', '已连接');
        appendProtocolEvent('system', 'WebSocket connected');
        messageInput.focus();
    });

    socket.addEventListener('message', (event) => {
        if (event.data instanceof ArrayBuffer) {
            appendProtocolEvent('incoming', {type: 'audio_frame', bytes: event.data.byteLength});
            audioPlayer.push(event.data);
            return;
        }
        try {
            handleIncoming(JSON.parse(event.data));
        } catch {
            appendProtocolEvent('incoming', event.data);
        }
    });

    socket.addEventListener('close', async (event) => {
        appendProtocolEvent('system', `WebSocket closed (${event.code})`);
        markStreamingMessagesInterrupted();
        pendingEvents.clear();
        audioPlayer.reset();
        await microphone.stop(false);
        socket = null;
        setStatus('offline', '离线');
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
    };
    pendingEvents.add(payload.event_id);
    appendMessage('user', text, payload.event_id.split('-').slice(-2).join('-'));
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
stopAudioButton.addEventListener('click', () => audioPlayer.stop());
micButton.addEventListener('click', async () => {
    if (microphone.active) {
        await microphone.stop();
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
connect();
