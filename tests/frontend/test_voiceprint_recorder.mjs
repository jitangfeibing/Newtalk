import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const voiceprintSource = readFileSync(new URL('../../web/voiceprint-recorder.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');

test('enrollment requests the same microphone processing as live conversation', async () => {
    const requests = [];
    const stopBeforeRecording = new Error('capture constraints only');
    const context = vm.createContext({
        navigator: {mediaDevices: {async getUserMedia(constraints) {
            requests.push(JSON.parse(JSON.stringify(constraints)));
            throw stopBeforeRecording;
        }}},
        window: {dispatchEvent() {}, AudioWorkletNode: class {}},
        CustomEvent: class {},
    });
    vm.runInContext(voiceprintSource.replace(/^export /gm, ''), context);
    const start = appSource.indexOf('    async start(format) {', appSource.indexOf('class MicrophoneRecorder'));
    const end = appSource.indexOf('\n    async stop(', start);
    assert.ok(start >= 0 && end > start);
    vm.runInContext(`globalThis.ChatRecorder = class {${appSource.slice(start, end)}}`, context);
    await assert.rejects(context.recordVoiceprintSample(), error => error === stopBeforeRecording);
    await assert.rejects(new context.ChatRecorder().start({
        codec: 'pcm_s16le', sample_rate: 16000, channels: 1,
    }), error => error === stopBeforeRecording);
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0], requests[1]);
    assert.deepEqual(requests[0], {audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    }});
});

test('enrollment still produces four-second PCM16 WAV and releases capture resources', async () => {
    let node;
    let trackStops = 0;
    let contextCloses = 0;
    let disconnections = 0;
    const modules = [];
    const context = vm.createContext({
        Blob,
        navigator: {mediaDevices: {async getUserMedia() {
            return {getTracks: () => [{stop() { trackStops += 1; }}]};
        }}},
        window: {
            dispatchEvent() {}, clearTimeout() {},
            setTimeout(callback, delay) {
                assert.equal(delay, 4000);
                queueMicrotask(callback);
                return 1;
            },
            AudioContext: class {
                destination = {};
                audioWorklet = {async addModule(path) { modules.push(path); }};
                createMediaStreamSource() {
                    return {connect() {}, disconnect() { disconnections += 1; }};
                }
                async resume() {
                    for (let index = 0; index < 200; index += 1) {
                        node.port.onmessage({data: {
                            type: 'audio_frame', buffer: new Int16Array(320).fill(3277).buffer,
                        }});
                    }
                }
                async close() { contextCloses += 1; }
            },
        },
        CustomEvent: class {},
        AudioWorkletNode: class {
            constructor() { this.port = {}; node = this; }
            connect() {}
            disconnect() { disconnections += 1; }
        },
    });
    vm.runInContext(voiceprintSource.replace(/^export /gm, ''), context);
    const progress = [];
    const sample = await context.recordVoiceprintSample(value => progress.push(value));
    const wav = new DataView(await sample.blob.arrayBuffer());
    assert.equal(wav.getUint16(22, true), 1);
    assert.equal(wav.getUint32(24, true), 16000);
    assert.equal(wav.getUint16(34, true), 16);
    assert.equal(wav.getUint32(40, true), 128000);
    assert.equal(sample.durationSeconds, 4);
    assert.equal(sample.quality.state, 'good');
    assert.equal(progress.at(-1), 1);
    assert.deepEqual(modules, ['/mic-recorder-worklet.js']);
    assert.equal(trackStops, 1);
    assert.equal(contextCloses, 1);
    assert.equal(disconnections, 2);
});
