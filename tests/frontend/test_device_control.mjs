import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {createDeviceControls} from '../../web/device-control.js';

function setup(overrides = {}) {
    const calls = [], results = [], statuses = [];
    const controls = createDeviceControls({
        stopReply: notify => calls.push(['stop', notify]),
        stopMicrophone: notify => calls.push(['mic', notify]),
        stopCamera: () => false,
        closeConnection: () => calls.push(['close']),
        sendResult: event => results.push(event),
        showStatus: message => statuses.push(message),
        isCurrent: () => true,
        ...overrides,
    });
    return {controls, calls, results, statuses};
}

test('server commands execute once and report their actual result', async () => {
    const {controls, calls, results} = setup();
    const request = {request_id: 'r1', action: 'microphone_off'};
    await Promise.all([controls.handle(request), controls.handle(request)]);
    assert.deepEqual(calls, [['mic', true]]);
    assert.deepEqual(results, [{
        type: 'device_control_result', request_id: 'r1', action: 'microphone_off', status: 'completed',
    }]);
});

test('the stop button notifies the server while a server command does not echo', async () => {
    const {controls, calls} = setup();
    await controls.execute('stop_reply');
    await controls.handle({request_id: 'r1', action: 'stop_reply'});
    assert.deepEqual(calls, [['stop', true], ['stop', false]]);
});

test('unimplemented camera reports not active instead of pretending to stop hardware', async () => {
    const {controls, results, statuses} = setup();
    await controls.handle({request_id: 'r1', action: 'camera_off'});
    assert.equal(results[0].status, 'not_active');
    assert.equal(statuses.at(-1), '摄像头未开启');
});

test('disconnect releases inputs and reports acceptance before closing', async () => {
    const order = [];
    const {controls} = setup({
        stopReply: notify => order.push(['stop', notify]),
        stopMicrophone: notify => order.push(['mic', notify]),
        stopCamera: () => order.push(['camera']),
        sendResult: event => order.push(['result', event.status]),
        closeConnection: () => order.push(['close']),
    });
    await controls.handle({request_id: 'r1', action: 'disconnect'});
    assert.deepEqual(order, [
        ['stop', false], ['mic', false], ['camera'], ['result', 'accepted'], ['close'],
    ]);
});

test('disconnect still closes if one resource cleanup fails', async () => {
    const {controls, calls, results} = setup({stopMicrophone() { throw Error('failed'); }});
    await controls.handle({request_id: 'r1', action: 'disconnect'});
    assert.equal(results[0].status, 'failed');
    assert.deepEqual(calls.at(-1), ['close']);
});

test('a delayed old command cannot send a result or close a new connection', async () => {
    let release, entered;
    const gate = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { entered = resolve; });
    const {controls, calls, results} = setup({
        async stopMicrophone() { entered(); await gate; },
    });
    const task = controls.handle({request_id: 'r1', action: 'disconnect'});
    await started;
    controls.dispose();
    release();
    await task;
    assert.deepEqual(results, []);
    assert.ok(!calls.some(([action]) => action === 'close'));
});

test('invalid actions and uncorrelated status messages do not execute', async () => {
    const {controls, calls, results, statuses} = setup();
    await controls.handle({request_id: 'r1', action: 'delete_all'});
    await controls.handle({action: 'disconnect'});
    controls.status({request_id: 'forged', status: 'unknown'});
    assert.deepEqual(calls, []);
    assert.deepEqual(results, []);
    assert.deepEqual(statuses, []);
});

test('late audio context shutdown does not clear a new microphone capture', async () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf('class MicrophoneRecorder {');
    const end = source.indexOf('\nconst microphone =', start);
    const context = vm.createContext({
        audioDiagnostics: {finish() {}}, micStatus: {dataset: {}}, micButton: {},
        updateVoiceProcessing() {}, resetVoiceActivityDiagnostics() {}, socket: null,
    });
    vm.runInContext(`${source.slice(start, end)}\nglobalThis.Recorder = MicrophoneRecorder;`, context);
    let release, stopped = 0;
    const closing = new Promise(resolve => { release = resolve; });
    const recorder = new context.Recorder();
    recorder.active = true;
    recorder.stream = {getTracks: () => [{stop() { stopped += 1; }}]};
    recorder.context = {close: () => closing};
    const task = recorder.stop(false);
    assert.equal(stopped, 1);
    assert.equal(recorder.stream, null);
    const replacement = {};
    recorder.context = replacement;
    recorder.active = true;
    release();
    await task;
    assert.equal(recorder.context, replacement);
    assert.equal(recorder.active, true);
});

test('disconnect closes the local socket without waiting for a server reply', () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf('function closeConnection(connection) {');
    const end = source.indexOf('\nfunction connect()', start);
    const events = [];
    const socket = {
        readyState: 1,
        send: data => events.push(JSON.parse(data).type),
        close: code => events.push(code),
    };
    const context = vm.createContext({
        socket, reminders: null, music: null, WebSocket: {OPEN: 1}, nextEventId: () => 'close-1',
        appendProtocolEvent() {}, setStatus() {},
    });
    vm.runInContext(source.slice(start, end), context);
    context.closeConnection(socket);
    assert.deepEqual(events, ['close', 1000]);
    context.closeConnection({readyState: 1});
    assert.deepEqual(events, ['close', 1000]);
});
