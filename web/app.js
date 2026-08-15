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

let socket = null;
let eventSequence = 0;
const pendingEvents = new Set();
const messagesByTurn = new Map();

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

    if (payload.type === 'turn_started') {
        pendingEvents.delete(payload.event_id);
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
        return;
    }

    if (payload.type === 'error' && payload.event_id && pendingEvents.has(payload.event_id)) {
        pendingEvents.delete(payload.event_id);
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

    socket.addEventListener('open', () => {
        setStatus('online', '已连接');
        appendProtocolEvent('system', 'WebSocket connected');
        messageInput.focus();
    });

    socket.addEventListener('message', (event) => {
        try {
            handleIncoming(JSON.parse(event.data));
        } catch {
            appendProtocolEvent('incoming', event.data);
        }
    });

    socket.addEventListener('close', (event) => {
        appendProtocolEvent('system', `WebSocket closed (${event.code})`);
        markStreamingMessagesInterrupted();
        pendingEvents.clear();
        socket = null;
        setStatus('offline', '离线');
    });

    socket.addEventListener('error', () => {
        appendProtocolEvent('system', 'WebSocket connection error');
        setStatus('error', '连接异常');
    });
}

chatForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = messageInput.value.trim();
    if (!text || socket?.readyState !== WebSocket.OPEN) return;

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

endpointElement.textContent = window.location.protocol === 'file:' ? 'HTTP runtime required' : websocketUrl();
connect();
