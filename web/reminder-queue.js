// 当前聊天结束后先播提醒；播报一旦开始，普通说话和新一轮聊天不抢占。
export function createReminderQueue({play, report, onActive = () => {}, now = Date.now, schedule = setTimeout, unschedule = clearTimeout}) {
    const busy = new Set(), seen = new Set();
    let connected = false, active = null, waiting = [], timer = null;

    function finish(job, status) {
        if (job.finished) return;
        job.finished = true;
        report(job.item, status);
    }
    function interrupt(job, status = 'interrupted') {
        finish(job, status);
        job.abort.abort();
    }
    function pump() {
        if (timer !== null) unschedule(timer);
        timer = null;
        const pending = [...waiting, ...(active && !active.started && !active.finished ? [active] : [])];
        for (const job of pending) {
            if (now() >= job.keepAliveAt) {
                job.keepAliveAt = now() + 15000;
                report(job.item, 'waiting');
            }
        }
        if (pending.length && connected) timer = schedule(pump,
            Math.max(1, Math.min(...pending.map(job => job.keepAliveAt)) - now()));
        if (!connected || busy.size || active || !waiting.length) return;
        const job = waiting.shift();
        active = job;
        onActive(true);
        const watchdog = schedule(() => interrupt(job, 'failed'), 65000);
        Promise.resolve().then(() => {
            if (job.abort.signal.aborted) throw new DOMException('Aborted', 'AbortError');
            return play(job.item, job.abort.signal, () => {
                if (!job.finished && active === job) {
                    job.started = true;
                    report(job.item, 'playing');
                }
            });
        }).then(() => finish(job, 'completed'), () => finish(job, job.abort.signal.aborted ? 'interrupted' : 'failed'))
            .finally(() => {
                unschedule(watchdog);
                if (active === job) active = null;
                onActive(false);
                pump();
            });
    }
    function stop() {
        for (const job of waiting) finish(job, 'interrupted');
        waiting = [];
        if (active) interrupt(active);
        pump();
    }
    return {
        connect(value) {
            if (!value) {
                connected = false;
                stop();
                busy.clear();
                seen.clear();
            } else { connected = true; pump(); }
        },
        enqueue(item) {
            if (!connected || !item.delivery_id || seen.has(item.delivery_id)) return;
            seen.add(item.delivery_id);
            if (seen.size > 256) seen.delete(seen.values().next().value);
            if (waiting.length >= 50) { report(item, 'expired'); return; }
            waiting.push({item, keepAliveAt: now() + 15000,
                abort: new AbortController(), started: false, finished: false});
            pump();
        },
        setBusy(key, value) {
            if (value) busy.add(key);
            else busy.delete(key);
            pump();
        },
        cancel(id) {
            waiting = waiting.filter(job => job.item.reminder_id !== id);
            if (active?.item.reminder_id === id) interrupt(active);
            pump();
        },
        deliveryEnded(deliveryId) {
            waiting = waiting.filter(job => job.item.delivery_id !== deliveryId);
            if (active?.item.delivery_id === deliveryId && !active.finished) {
                active.finished = true;
                active.abort.abort();
            }
            pump();
        },
        isPlaying: () => active !== null,
        stop,
    };
}
