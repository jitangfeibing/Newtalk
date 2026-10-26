import {createReminderQueue} from './reminder-queue.js?v=20261026.1';
import {createReminderPlayer} from './reminder-player.js?v=20261025.1';

const labels = {scheduled: '待提醒', triggered: '已触发', missed: '已错过（当时未连接）', cancelled: '已取消',
    pending: '等待本轮对话结束', waiting: '等待本轮对话结束', playing: '正在播放', completed: '已播完', interrupted: '播放已中断',
    failed: '播放失败，可手动重播', expired: '本次未播放，可手动重播', unknown: '播放结果未知，可手动重播'};
const formatTime = value => {
    // 产品固定北京时间，避免浏览器区域/时区覆盖把列表显示成 UTC。
    const date = new Date(Date.parse(value) + 8 * 3600000);
    const pad = number => String(number).padStart(2, '0');
    return `${date.getUTCFullYear()}年${date.getUTCMonth() + 1}月${date.getUTCDate()}日 ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
};

export function initializeReminders({onPlaybackActive = () => {}} = {}) {
    const form = document.querySelector('#reminderForm'), content = document.querySelector('#reminderContent');
    const due = document.querySelector('#reminderDue'), list = document.querySelector('#reminderList');
    const feedback = document.querySelector('#reminderFeedback'), banner = document.querySelector('#reminderBanner');
    const bannerText = document.querySelector('#reminderBannerText'), save = document.querySelector('#saveReminderButton');
    const cancelEdit = document.querySelector('#cancelReminderEdit'), state = document.querySelector('#reminderConnection');
    const player = createReminderPlayer();
    let editing = null, requestKey = crypto.randomUUID(), send = null, online = false, loadVersion = 0;
    const queue = createReminderQueue({
        onActive: onPlaybackActive,
        play: (item, signal, started) => player.play(item, signal, started),
        report(item, status) {
            send?.({type: 'reminder_playback', reminder_id: item.reminder_id, delivery_id: item.delivery_id, status});
            banner.hidden = false;
            bannerText.textContent = `${item.content} · ${labels[status]}`;
            if (!['playing', 'waiting'].includes(status)) void refresh();
        },
    });
    async function api(path = '', options = {}) {
        const response = await fetch(`/api/reminders${path}`, {cache: 'no-store', ...options,
            headers: {'Content-Type': 'application/json'}});
        const data = await response.json();
        if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '请检查提醒内容和时间');
        return data;
    }
    function resetForm() {
        form.reset(); editing = null; requestKey = crypto.randomUUID();
        save.textContent = '添加提醒'; cancelEdit.hidden = true;
    }
    function button(text, run) {
        const element = document.createElement('button');
        element.type = 'button'; element.className = 'button secondary compact'; element.textContent = text;
        element.addEventListener('click', async () => {
            element.disabled = true;
            try { await run(); await refresh(); }
            catch (error) { feedback.textContent = error.message; }
            finally { element.disabled = false; }
        });
        return element;
    }
    async function refresh() {
        const version = ++loadVersion;
        try {
            const data = await api();
            if (version !== loadVersion) return;
            list.replaceChildren();
            if (!data.items.length) {
                const empty = document.createElement('p'); empty.textContent = '还没有提醒。可以在这里添加，也可以在对话里说“五分钟后提醒我取快递”。';
                list.append(empty);
            }
            for (const item of data.items) {
                const row = document.createElement('li'), title = document.createElement('strong');
                title.textContent = item.content;
                const meta = document.createElement('p');
                const status = item.status === 'triggered' ? labels[item.delivery_status] || labels.triggered : labels[item.status];
                meta.textContent = `${formatTime(item.due_at)} · ${status}`;
                const actions = document.createElement('div'); actions.className = 'inline-actions';
                if (item.status === 'scheduled') actions.append(button('修改', () => {
                    editing = item.reminder_id; content.value = item.content;
                    due.value = new Date(Date.parse(item.due_at) + 8 * 3600000).toISOString().slice(0, 16);
                    save.textContent = '保存修改'; cancelEdit.hidden = false; content.focus();
                }));
                if (item.status !== 'scheduled' && item.status !== 'cancelled' && !['pending', 'playing'].includes(item.delivery_status)) {
                    const replay = button('重新播放', async () => {
                        if (!online) throw Error('请先在对话页连接');
                        if (!queue.isPlaying()) await player.unlock();
                        await api(`/${item.reminder_id}/replay`, {method: 'POST'});
                    });
                    actions.append(replay);
                }
                if (item.status !== 'cancelled') actions.append(button('取消提醒', async () => {
                    await api(`/${item.reminder_id}`, {method: 'DELETE'});
                    if (editing === item.reminder_id) resetForm();
                }));
                row.append(title, meta, actions); list.append(row);
            }
        } catch (error) { if (version === loadVersion) feedback.textContent = error.message; }
    }
    form.addEventListener('submit', async event => {
        event.preventDefault(); save.disabled = true;
        try {
            const body = {content: content.value.trim(), due_at: `${due.value}:00+08:00`};
            if (!editing) body.request_key = requestKey;
            await api(editing ? `/${editing}` : '', {method: editing ? 'PATCH' : 'POST', body: JSON.stringify(body)});
            feedback.textContent = '提醒已保存，到点时向连接中的网页播报。'; resetForm(); await refresh();
        } catch (error) { feedback.textContent = error.message; }
        finally { save.disabled = false; }
    });
    cancelEdit.addEventListener('click', resetForm);
    document.querySelector('#refreshRemindersButton').addEventListener('click', refresh);
    document.querySelector('#enableReminderAudio').addEventListener('click', async () => {
        if (queue.isPlaying()) { feedback.textContent = '提醒正在播报，请播完后再启用提示音。'; return; }
        try { await player.unlock(); feedback.textContent = '提示音已启用，请保持网页连接。'; }
        catch { feedback.textContent = '浏览器未允许播放，请再次点击启用提示音。'; }
    });
    document.querySelector('#stopReminderButton').addEventListener('click', () => queue.stop());
    document.querySelector('#dismissReminderButton').addEventListener('click', () => { banner.hidden = true; });
    window.addEventListener('newtalk:view-changed', event => { if (event.detail.view === 'tools') void refresh(); });
    window.addEventListener('focus', () => { if (!document.querySelector('#tools').hidden) void refresh(); });
    return {
        connect(sender) {
            send = sender; online = Boolean(sender); queue.connect(online);
            state.textContent = online ? '已连接 · 到点播报' : '未连接 · 到点会记为错过';
            void refresh();
        },
        handle(event) {
            if (event.type === 'reminder_due') {
                banner.hidden = false; bannerText.textContent = `提醒：${event.reminder.content}`;
                queue.enqueue(event.reminder); void refresh();
            } else if (event.type === 'reminder_cancelled') {
                queue.cancel(event.reminder_id); void refresh();
            } else if (event.type === 'reminder_delivery_ended') {
                queue.deliveryEnded(event.delivery_id); void refresh();
            } else if (event.type === 'reminders_changed') void refresh();
        },
        setBusy: queue.setBusy,
        stop: queue.stop,
    };
}
