import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

function worklet() {
    let Player;
    const events = [];
    const context = vm.createContext({
        AudioWorkletProcessor: class { port = {postMessage: event => events.push(event)}; },
        registerProcessor(name, Type) { Player = Type; },
        currentTime: 10, sampleRate: 24000,
    });
    vm.runInContext(readFileSync(new URL('../../web/pcm-player-worklet.js', import.meta.url), 'utf8'), context);
    const player = new Player();
    const send = data => player.port.onmessage({data});
    return {events, player, send};
}

test('silent PCM starts rendering but does not count as audible output', () => {
    const {events, player, send} = worklet();
    send({type: 'reset', stream_id: 'stream-1'});
    send({type: 'audio', buffer: new Int16Array([0, 0, 0]).buffer});
    send({type: 'play'});
    player.process([], [[new Float32Array(3)]]);
    assert.equal(events.filter(event => event.type === 'playback_started').length, 1);
    assert.equal(events.filter(event => event.type === 'playback_audible').length, 0);
    send({type: 'audio', buffer: new Int16Array([0, 0, 1000]).buffer});
    player.process([], [[new Float32Array(3)]]);
    const audible = events.find(event => event.type === 'playback_audible');
    assert.equal(audible?.stream_id, 'stream-1');
    assert.equal(audible.context_time, 10 + 2 / 24000);
    player.process([], [[new Float32Array(3)]]);
    assert.equal(events.filter(event => event.type === 'playback_audible').length, 1);
});

test('reset separates stream IDs and permits a new first audible sample', () => {
    const {events, player, send} = worklet();
    for (const stream of ['one', 'two']) {
        send({type: 'reset', stream_id: stream});
        send({type: 'audio', buffer: new Int16Array([1000]).buffer});
        send({type: 'play'});
        player.process([], [[new Float32Array(1)]]);
    }
    assert.deepEqual(events.filter(event => event.type === 'playback_audible').map(event => event.stream_id), ['one', 'two']);
});

test('microphone worklet attaches sample position and audio-clock time without changing PCM', () => {
    let Recorder;
    const frames = [];
    const context = vm.createContext({
        AudioWorkletProcessor: class { port = {postMessage: event => frames.push(event)}; },
        registerProcessor(name, Type) { Recorder = Type; },
        currentTime: 2, sampleRate: 16000,
    });
    vm.runInContext(readFileSync(new URL('../../web/mic-recorder-worklet.js', import.meta.url), 'utf8'), context);
    new Recorder().process([[new Float32Array(321).fill(0.5)]], [[new Float32Array(321)]]);
    assert.equal(frames.length, 1);
    assert.equal(frames[0].audio_end_ms, 20);
    assert.equal(frames[0].frame_end_time, 2.02);
    assert.equal(new Int16Array(frames[0].buffer).length, 320);
});

async function clocks() {
    const source = readFileSync(new URL('../../web/voice-latency.js', import.meta.url), 'utf8');
    return import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}

test('capture clock maps VAD sample positions onto browser monotonic time', async () => {
    const {captureOriginMs} = await clocks();
    const origin = captureOriginMs({audio_end_ms: 1000, frame_end_time: 4}, {currentTime: 4.1}, 5100);
    assert.ok(Math.abs(origin - 4000) < 0.0001);
    assert.equal(captureOriginMs({}, {currentTime: 4.1}, 5100), null);
});

test('output timestamp maps rendered sample to device-output estimate, with an explicit fallback', async () => {
    const {outputPerformanceTime} = await clocks();
    assert.deepEqual(outputPerformanceTime({getOutputTimestamp: () => ({contextTime: 9.9, performanceTime: 5000})}, 10, 5200),
        {atMs: 5100, clock: 'output_timestamp'});
    assert.deepEqual(outputPerformanceTime({currentTime: 10}, 10, 5200),
        {atMs: 5200, clock: 'render_clock'});
});

async function browserPlayer(telemetry) {
    const {outputPerformanceTime} = await clocks();
    const sent = [];
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const context = vm.createContext({
        turnTelemetry: new Map([['turn-1', telemetry]]),
        outputPerformanceTime, performance: {now: () => 1850},
        socket: {readyState: 1, send: text => sent.push(JSON.parse(text))}, WebSocket: {OPEN: 1},
        nextEventId: () => 'event-1', appendProtocolEvent() {},
        recordTurnMetric(turn, key, value) { telemetry[key] = value; },
    });
    vm.runInContext(source.slice(source.indexOf('class PcmPlayer {'), source.indexOf('\nconst audioPlayer =')), context);
    const player = vm.runInContext('new PcmPlayer()', context);
    player.context = {getOutputTimestamp: () => ({contextTime: 9.9, performanceTime: 1700})};
    player.active = {streamId: 'stream-1', turnId: 'turn-1', stopped: false};
    return {player, sent};
}

test('browser reports end-to-sound independently of Turn timing and ignores stale/duplicate events', async () => {
    const telemetry = {startedAt: 1000, lastVoiceAt: 200, speechEndedAt: 800, firstFrameAt: 1300};
    const {player, sent} = await browserPlayer(telemetry);
    player.handleWorkletEvent({type: 'playback_audible', stream_id: 'old-stream', context_time: 10});
    assert.equal(sent.length, 0);
    const event = {type: 'playback_audible', stream_id: 'stream-1', context_time: 10};
    player.handleWorkletEvent(event);
    player.handleWorkletEvent(event);
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].metrics, {turn_to_audible_ms: 800, speech_end_to_audible_ms: 1600,
        vad_to_audible_ms: 1000, tts_to_audible_ms: 500});
    assert.equal(telemetry.audibleMs, 800);
    assert.equal(telemetry.speechEndToAudibleMs, 1600);
});

test('missing capture reference is not converted to a zero endpoint and stopped audio is not counted', async () => {
    const {player, sent} = await browserPlayer({startedAt: 1000});
    player.active.stopped = true;
    const event = {type: 'playback_audible', stream_id: 'stream-1', context_time: 10};
    player.handleWorkletEvent(event);
    assert.equal(sent.length, 0);
    player.active.stopped = false;
    player.handleWorkletEvent(event);
    assert.deepEqual(sent[0].metrics, {turn_to_audible_ms: 800});
});
