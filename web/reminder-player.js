export function createReminderPlayer({audio = new Audio(), request = fetch, urls = URL} = {}) {
    audio.preload = 'auto';
    return {
        async unlock() {
            // 用户主动点击后解锁同一个播放元素；没有权限时仍保留文字提醒和手动重播。
            audio.src = 'data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQIAAAAAAA==';
            await audio.play();
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
        },
        async play(item, signal, onStarted) {
            const response = await request(`/api/reminders/${encodeURIComponent(item.reminder_id)}/audio?delivery_id=${encodeURIComponent(item.delivery_id)}`,
                {signal, cache: 'no-store'});
            if (!response.ok) throw Error('提醒语音暂不可用');
            const blob = await response.blob();
            if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
            const url = urls.createObjectURL(blob);
            try {
                await new Promise((resolve, reject) => {
                    let started = false;
                    const clean = () => {
                        audio.removeEventListener('playing', playing);
                        audio.removeEventListener('ended', ended);
                        audio.removeEventListener('error', failed);
                        signal.removeEventListener('abort', aborted);
                    };
                    const playing = () => { if (!started) { started = true; onStarted(); } };
                    const ended = () => { clean(); resolve(); };
                    const failed = () => { clean(); reject(Error('提醒播放失败')); };
                    const aborted = () => { audio.pause(); clean(); reject(new DOMException('Aborted', 'AbortError')); };
                    audio.addEventListener('playing', playing);
                    audio.addEventListener('ended', ended);
                    audio.addEventListener('error', failed);
                    signal.addEventListener('abort', aborted, {once: true});
                    audio.src = url;
                    Promise.resolve(audio.play()).catch(failed);
                });
            } finally {
                audio.pause();
                audio.removeAttribute('src');
                audio.load();
                urls.revokeObjectURL(url);
            }
        },
    };
}
