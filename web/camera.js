export function createCamera({video, getUserMedia, createCanvas, render = () => {}}) {
    let stream = null;
    let opening = false;
    let generation = 0;
    let send = null;
    let currentRequest = null;
    const seen = new Set();
    const cancelledTurns = new Set();
    const publish = () => send?.({type: 'camera_state', opened: Boolean(stream), revision: generation});
    const show = (state, message) => render({state, message, opened: Boolean(stream), opening});

    function stop() {
        const active = Boolean(stream || opening);
        generation += 1;
        opening = false;
        currentRequest = null;
        const previous = stream;
        stream = null;
        video.srcObject = null;
        previous?.getTracks().forEach(track => track.stop());
        publish();
        show('off', '摄像头已关闭');
        return active;
    }

    async function start() {
        if (stream || opening) return;
        const token = ++generation;
        opening = true;
        show('opening', '等待摄像头权限…');
        let acquired = null;
        try {
            acquired = await getUserMedia({video: {width: {ideal: 1280}, height: {ideal: 720}, facingMode: 'user'}, audio: false});
            // 权限弹窗关闭得很晚时，也不能重新打开已经关闭或断开的预览。
            if (token !== generation) {
                acquired.getTracks().forEach(track => track.stop());
                return;
            }
            stream = acquired;
            opening = false;
            video.srcObject = stream;
            stream.getVideoTracks().forEach(track => track.addEventListener('ended', () => {
                if (token === generation) stop();
            }));
            publish();
            show('ready', '本地预览已开启');
            await video.play();
        } catch (error) {
            if (token !== generation) return;
            stop();
            show('error', error?.name === 'NotAllowedError' ? '未获得摄像头权限，可点击重试' : '摄像头不可用，请检查设备后重试');
        }
    }

    function capture(event) {
        if (!send || typeof event.request_id !== 'string' || typeof event.turn_id !== 'string'
            || cancelledTurns.has(event.turn_id) || event.camera_revision !== generation
            || !Number.isFinite(event.expires_at) || event.expires_at <= Date.now() || seen.has(event.request_id)) return;
        seen.add(event.request_id);
        if (seen.size > 32) seen.delete(seen.values().next().value);
        currentRequest = {id: event.request_id, turn: event.turn_id};
        const result = {type: 'camera_frame', request_id: event.request_id, turn_id: event.turn_id};
        try {
            if (!stream || video.readyState < 2 || !video.videoWidth || !video.videoHeight) throw new Error('not_ready');
            const canvas = createCanvas();
            let encoded;
            // 大画面压缩后仍超限时只在本地缩小一次，始终只发送一张图片。
            for (const edge of [Math.min(1280, event.max_edge || 1280), 640]) {
                const scale = Math.min(1, edge / Math.max(video.videoWidth, video.videoHeight));
                canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
                canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
                canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
                encoded = canvas.toDataURL('image/jpeg', 0.76).split(',')[1];
                if (encoded && encoded.length <= Math.floor(Math.min(512000, event.max_bytes || 512000) / 3) * 4) break;
                encoded = null;
            }
            if (!encoded) throw new Error('too_large');
            send({...result, mime_type: 'image/jpeg', image_base64: encoded,
                width: canvas.width, height: canvas.height, captured_at: Date.now()});
            show('analyzing', '正在分析这一帧…');
        } catch {
            send({...result, status: 'error'});
            show('unavailable', '这次没有取得画面，可稍后再试');
        }
    }

    return {
        start, stop,
        get active() { return Boolean(stream || opening); },
        connect(sender) {
            if (!sender) { stop(); send = null; seen.clear(); cancelledTurns.clear(); return; }
            send = sender;
            publish();
        },
        handle(event) {
            if (event.type === 'camera_capture') capture(event);
            else if (event.type === 'turn_cancelled') {
                cancelledTurns.add(event.turn_id);
                if (cancelledTurns.size > 32) cancelledTurns.delete(cancelledTurns.values().next().value);
                if (event.turn_id === currentRequest?.turn) {
                    currentRequest = null;
                    show(stream ? 'ready' : 'off', stream ? '本次看图已取消，预览保持开启' : '摄像头已关闭');
                }
            } else if (event.type === 'vision_status') {
                if (!stream || cancelledTurns.has(event.turn_id)) return;
                if (event.status === 'capturing') currentRequest = {id: event.request_id, turn: event.turn_id};
                if (event.request_id !== currentRequest?.id) return;
                const labels = {capturing: '正在截取当前画面…', analyzing: '正在分析这一帧…',
                    completed: '本次分析完成', unavailable: '这次没能看到画面，可稍后再试'};
                show(event.status, labels[event.status] || '本地预览已开启');
            }
        },
    };
}

export function initializeCamera() {
    const button = document.querySelector('#cameraButton');
    const video = document.querySelector('#cameraPreview');
    const status = document.querySelector('#cameraStatus');
    const placeholder = document.querySelector('#cameraPlaceholder');
    const controller = createCamera({video,
        getUserMedia: options => navigator.mediaDevices.getUserMedia(options),
        createCanvas: () => document.createElement('canvas'),
        render: ({message, opened, opening}) => {
            status.textContent = message;
            video.hidden = !opened;
            placeholder.hidden = opened;
            button.textContent = opened || opening ? '关闭摄像头' : '开启摄像头';
            button.setAttribute('aria-pressed', String(opened || opening));
        },
    });
    button.addEventListener('click', () => controller.active ? controller.stop() : void controller.start());
    window.addEventListener('pagehide', () => controller.connect(null));
    return controller;
}
