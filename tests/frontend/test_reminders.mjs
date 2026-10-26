import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {createReminderQueue} from '../../web/reminder-queue.js';
import {createReminderPlayer} from '../../web/reminder-player.js';

function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; }
const item = id => ({reminder_id: 'reminder', delivery_id: id, content: '取快递'});
async function flush() { for (let index = 0; index < 10; index += 1) await Promise.resolve(); }
function setup() {
    const reports = [], plays = [], timers = new Map();
    let time = 0, next = 0;
    const queue = createReminderQueue({
        now: () => time, schedule: callback => { timers.set(++next, callback); return next; }, unschedule: id => timers.delete(id),
        report: (entry, status) => reports.push([entry.delivery_id, status]),
        play(entry, signal, started) {
            const finished = deferred();
            plays.push({entry, signal, finished}); started();
            signal.addEventListener('abort', () => finished.reject(Error('aborted')), {once: true});
            return finished.promise;
        },
    });
    queue.connect(true);
    return {queue, reports, plays, timers, advance(value) { time = value; }};
}

test('reminders wait for both generation and real chat audio drain, deduplicate and play once', async () => {
    const {queue, reports, plays} = setup();
    queue.setBusy('generation', true); queue.setBusy('audio', true);
    queue.enqueue(item('d1')); queue.enqueue(item('d1'));
    queue.setBusy('generation', false); await flush(); assert.equal(plays.length, 0);
    queue.setBusy('audio', false); await flush(); assert.equal(plays.length, 1);
    plays[0].finished.resolve(); await flush();
    assert.deepEqual(reports, [['d1', 'playing'], ['d1', 'completed']]);
    queue.enqueue(item('d1')); await flush(); assert.equal(plays.length, 1);
    queue.connect(false);
});

test('user speech and a new chat turn cannot interrupt a reminder', async () => {
    const {queue, reports, plays} = setup();
    queue.enqueue(item('d1')); await flush(); queue.setBusy('user', true);
    queue.setBusy('generation', true);
    assert.equal(plays[0].signal.aborted, false);
    assert.equal(reports.some(([, status]) => status === 'interrupted'), false);
    plays[0].finished.resolve();
    queue.setBusy('user', false); await flush();
    assert.deepEqual(reports, [['d1', 'playing'], ['d1', 'completed']]);
    assert.equal(plays.length, 1); queue.connect(false);
});

test('a long chat does not discard a reminder after thirty seconds', async () => {
    const {queue, reports, plays, advance} = setup();
    queue.setBusy('audio', true); queue.enqueue(item('d1'));
    advance(31000); queue.setBusy('audio', false); await flush();
    assert.equal(plays.length, 1);
    assert.equal(reports.some(([, status]) => status === 'expired'), false);
    plays[0].finished.resolve(); await flush();
    assert.equal(reports.at(-1)[1], 'completed'); queue.connect(false);
});

test('queued reminders renew their server delivery while a long chat is still playing', () => {
    const {queue, reports, timers, advance} = setup();
    queue.setBusy('audio', true); queue.enqueue(item('d1'));
    advance(16000);
    for (const callback of [...timers.values()]) callback();
    assert.deepEqual(reports, [['d1', 'waiting']]);
    queue.connect(false);
});

test('a waiting reply releases chat generation busy state so the reminder can actually start', () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf('function handleIncoming(payload) {');
    const end = source.indexOf('\nfunction renderDialogueSnapshot(', start);
    const states = [], busy = [], bubble = {};
    const context = vm.createContext({
        music: null, appendProtocolEvent() {}, reminders: {setBusy: (...args) => busy.push(args)},
        messagesByTurn: new Map([['turn', bubble]]),
        setMessageState: (...args) => states.push(args),
    });
    vm.runInContext(source.slice(start, end), context);
    context.handleIncoming({type: 'turn_waiting', turn_id: 'turn', reason: 'reminder_playback'});
    assert.deepEqual(busy, [['generation', false], ['user', false]]);
    assert.deepEqual(states, [[bubble, 'waiting', '提醒播报后回复']]);
});

test('server terminal notification removes only the matching delivery and releases the queue', async () => {
    const {queue, reports, plays} = setup();
    queue.enqueue(item('d1')); queue.enqueue(item('d2')); await flush();
    queue.deliveryEnded('stale'); assert.equal(plays[0].signal.aborted, false);
    queue.deliveryEnded('d1'); await flush();
    assert.equal(plays[0].signal.aborted, true);
    assert.equal(plays[1].entry.delivery_id, 'd2');
    plays[1].finished.resolve(); await flush();
    assert.deepEqual(reports, [['d1', 'playing'], ['d2', 'playing'], ['d2', 'completed']]);
    queue.connect(false);
});

test('disconnect aborts in-flight work and manual replay with a new id works after reconnect', async () => {
    const {queue, reports, plays} = setup();
    queue.enqueue(item('d1')); await flush(); queue.connect(false);
    assert.equal(plays[0].signal.aborted, true);
    queue.connect(true); queue.enqueue(item('d2')); await flush();
    assert.equal(plays.length, 2); plays[1].finished.resolve(); await flush();
    assert.deepEqual(reports, [['d1', 'playing'], ['d1', 'interrupted'], ['d2', 'playing'], ['d2', 'completed']]);
    queue.connect(false);
});

test('cancel removes queued reminders and stops an active one', async () => {
    const {queue, plays} = setup(); queue.setBusy('audio', true);
    queue.enqueue(item('d1')); queue.cancel('reminder'); queue.setBusy('audio', false); await flush();
    assert.equal(plays.length, 0);
    queue.enqueue(item('d2')); await flush(); queue.cancel('reminder');
    assert.equal(plays[0].signal.aborted, true); await flush(); queue.connect(false);
});

class FakeAudio extends EventTarget {
    constructor() { super(); this.playCalls = 0; this.pauseCalls = 0; }
    play() { this.playCalls += 1; return Promise.resolve(); }
    pause() { this.pauseCalls += 1; }
    removeAttribute() {}
    load() {}
}

test('late audio response after disconnect never starts playback', async () => {
    const response = deferred(), audio = new FakeAudio(), abort = new AbortController();
    const player = createReminderPlayer({audio, request: () => response.promise});
    const playback = player.play(item('d1'), abort.signal, () => assert.fail('must not start'));
    abort.abort(); response.resolve({ok: true, blob: () => Promise.resolve(new Blob())});
    await assert.rejects(playback, {name: 'AbortError'}); assert.equal(audio.playCalls, 0);
});

test('audio reports actual playing and cleans blob and element on completion', async () => {
    const audio = new FakeAudio(), revoked = [], abort = new AbortController(); let started = 0;
    const player = createReminderPlayer({audio, request: async () => ({ok: true, blob: async () => new Blob()}),
        urls: {createObjectURL: () => 'blob:reminder', revokeObjectURL: url => revoked.push(url)}});
    const playback = player.play(item('d1'), abort.signal, () => { started += 1; });
    await flush(); assert.equal(started, 0);
    audio.dispatchEvent(new Event('playing')); audio.dispatchEvent(new Event('playing'));
    assert.equal(started, 1); audio.dispatchEvent(new Event('ended')); await playback;
    assert.deepEqual(revoked, ['blob:reminder']); assert.ok(audio.pauseCalls);
});
