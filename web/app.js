const statusElement = document.querySelector('#connectionStatus');
const statusLabel = document.querySelector('#statusLabel');
const endpointElement = document.querySelector('#endpoint');
const connectionButton = document.querySelector('#connectionButton');
const pingButton = document.querySelector('#pingButton');
const eventLog = document.querySelector('#eventLog');

let socket = null;
let eventSequence = 0;

function websocketUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${window.location.host}/ws`;
}

function setStatus(state, label) {
    statusElement.dataset.state = state;
    statusLabel.textContent = label;
    const connected = state === 'online';
    pingButton.disabled = !connected;
    connectionButton.disabled = state === 'connecting' || state === 'closing';
    connectionButton.textContent = connected ? '断开' : state === 'closing' ? '断开中' : '连接';
}

function appendEvent(direction, payload) {
    const item = document.createElement('li');
    const badge = document.createElement('span');
    const content = document.createElement('pre');

    badge.className = `event-direction ${direction}`;
    badge.textContent = direction === 'incoming' ? 'IN' : direction === 'outgoing' ? 'OUT' : 'SYS';
    content.textContent = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
    item.append(badge, content);
    eventLog.prepend(item);
}

function disconnect() {
    if (socket?.readyState === WebSocket.OPEN) {
        const event = {
            type: 'close',
            event_id: `web-${Date.now()}-${++eventSequence}`,
        };
        socket.send(JSON.stringify(event));
        appendEvent('outgoing', event);
        setStatus('closing', '断开中');
    }
}

function connect() {
    if (window.location.protocol === 'file:') {
        appendEvent('system', '请通过 HTTP 启动页面，不要使用 file:// 打开 index.html');
        setStatus('error', '页面地址错误');
        return;
    }

    if (socket) {
        if (socket.readyState === WebSocket.OPEN) {
            disconnect();
        }
        return;
    }

    setStatus('connecting', '连接中');
    socket = new WebSocket(websocketUrl());

    socket.addEventListener('open', () => {
        setStatus('online', '已连接');
        appendEvent('system', 'WebSocket connected');
    });

    socket.addEventListener('message', (event) => {
        try {
            appendEvent('incoming', JSON.parse(event.data));
        } catch {
            appendEvent('incoming', event.data);
        }
    });

    socket.addEventListener('close', (event) => {
        appendEvent('system', `WebSocket closed (${event.code})`);
        socket = null;
        setStatus('offline', '离线');
    });

    socket.addEventListener('error', () => {
        appendEvent('system', 'WebSocket connection error');
        setStatus('error', '连接异常');
    });
}

connectionButton.addEventListener('click', connect);
pingButton.addEventListener('click', () => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const event = {
        type: 'ping',
        event_id: `web-${Date.now()}-${++eventSequence}`,
    };
    socket.send(JSON.stringify(event));
    appendEvent('outgoing', event);
});

endpointElement.textContent = window.location.protocol === 'file:' ? 'HTTP runtime required' : websocketUrl();
connect();
