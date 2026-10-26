const actions = new Set(['stop_reply', 'microphone_off', 'camera_off', 'disconnect']);
const completedMessages = {
    stop_reply: '回复已停止', microphone_off: '麦克风已关闭',
    camera_off: '摄像头已关闭', disconnect: '正在断开连接',
};

export function createDeviceControls({
    stopReply, stopMicrophone, stopCamera, closeConnection, sendResult, showStatus,
    isCurrent, timeoutMs = 2000,
}) {
    let active = true;
    let latestRequestId = null;
    const seen = new Map();
    const current = () => active && isCurrent();

    async function bounded(operation) {
        let timer;
        try {
            return await Promise.race([
                operation(),
                new Promise((_, reject) => {
                    timer = setTimeout(() => reject(Error('cleanup timeout')), timeoutMs);
                }),
            ]);
        } finally {
            clearTimeout(timer);
        }
    }

    async function execute(action, request = null) {
        if (!actions.has(action) || !current()) return;
        latestRequestId = request?.request_id ?? null;
        let status = 'completed';
        try {
            if (action === 'stop_reply') {
                await bounded(() => stopReply(!request));
            } else if (action === 'microphone_off') {
                await bounded(() => stopMicrophone(true));
            } else if (action === 'camera_off') {
                if (await bounded(() => stopCamera()) === false) status = 'not_active';
            } else {
                // 各项独立清理；一项失败也继续关闭其余资源和连接。
                for (const cleanup of [() => stopReply(false), () => stopMicrophone(false), stopCamera]) {
                    if (!current()) return;
                    try { await bounded(cleanup); }
                    catch { status = 'failed'; }
                }
                if (status !== 'failed') status = 'accepted';
            }
        } catch {
            status = 'failed';
        }
        if (!current()) return;
        const message = status === 'failed' ? '控制执行失败，请使用页面按钮重试'
            : status === 'not_active' ? '摄像头未开启' : completedMessages[action];
        showStatus(message);
        try {
            if (request) sendResult({
                type: 'device_control_result', request_id: request.request_id, action, status,
            });
        } finally {
            if (action === 'disconnect' && current()) closeConnection();
        }
    }

    function handle(request) {
        if (!current() || typeof request.request_id !== 'string' || !request.request_id
                || request.request_id.length > 128 || !actions.has(request.action)) return;
        if (seen.has(request.request_id)) return seen.get(request.request_id);
        const task = execute(request.action, request);
        seen.set(request.request_id, task);
        // 连接内有界去重；服务端不重试已经结束的请求。
        if (seen.size > 128) seen.delete(seen.keys().next().value);
        return task;
    }

    function status(event) {
        if (!current() || !latestRequestId || event.request_id !== latestRequestId) return;
        if (event.status === 'unknown') showStatus('尚未确认控制结果，请检查页面状态');
    }

    return {execute, handle, status, dispose() { active = false; seen.clear(); }};
}
