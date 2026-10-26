import assert from 'node:assert/strict';
import {test} from 'node:test';
import {DiagnosticRecording, initializeAudioDiagnostics} from '../../web/audio-diagnostics.js';

test('only captures explicitly enabled frames and copies the outgoing buffer', async () => {
    const recording = new DiagnosticRecording();
    const pcm = new Int16Array([-32768, 123, 32767]);
    recording.push(pcm.buffer);
    assert.equal(recording.finish(), null);
    recording.start();
    recording.push(pcm.buffer);
    pcm.fill(0);
    const blob = recording.finish();
    const wav = new DataView(await blob.arrayBuffer());
    assert.equal(wav.getUint32(24, true), 16000);
    assert.equal(wav.getUint16(22, true), 1);
    assert.equal(wav.getUint32(40, true), 6);
    assert.equal(wav.getInt16(44, true), -32768);
    assert.equal(wav.getInt16(46, true), 123);
    assert.equal(wav.getInt16(48, true), 32767);
    assert.equal(recording.finish(), null);
});

test('stops at ten seconds without retaining further frames; clear releases samples', async () => {
    const recording = new DiagnosticRecording();
    recording.start();
    for (let index = 0; index < 501; index += 1) {
        recording.push(new Int16Array(320).buffer);
    }
    assert.equal(recording.active, false);
    assert.equal(recording.sampleCount, 160000);
    assert.equal((await recording.finish().arrayBuffer()).byteLength, 320044);
    recording.start();
    recording.push(new Int16Array(320).buffer);
    recording.clear();
    assert.equal(recording.active, false);
    assert.equal(recording.sampleCount, 0);
    assert.equal(recording.finish(), null);
});

test('UI guards capture, closes microphone on every playback, and releases previews', async (t) => {
    class Element {
        handlers = new Map();
        hidden = true;
        disabled = false;
        addEventListener(name, fn) { this.handlers.set(name, fn); }
        removeAttribute(name) { delete this[name]; }
        pause() {}
        load() {}
        async emit(name) { await this.handlers.get(name)?.(); }
        async play() { await this.emit('play'); }
    }
    const ids = ['Start', 'Stop', 'Clear'].map(name => `diagnosticRecord${name}`)
        .concat(['diagnosticRecordingPlayer', 'diagnosticRecordingDownload', 'diagnosticRecordingStatus']);
    const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
    const windowHandlers = new Map();
    let timer;
    const previousDocument = globalThis.document;
    const previousWindow = globalThis.window;
    globalThis.document = {querySelector: selector => elements[selector.slice(1)]};
    globalThis.window = {
        setTimeout: fn => { timer = fn; return 1; },
        clearTimeout: () => { timer = null; },
        addEventListener: (name, fn) => windowHandlers.set(name, fn),
    };
    t.after(() => {
        globalThis.document = previousDocument;
        globalThis.window = previousWindow;
    });
    const revoked = [];
    t.mock.method(URL, 'createObjectURL', () => 'blob:test-preview');
    t.mock.method(URL, 'revokeObjectURL', url => revoked.push(url));
    let micActive = false;
    let playbackStops = 0;
    const ui = initializeAudioDiagnostics({
        isMicrophoneActive: () => micActive,
        beforePlayback: async () => { playbackStops += 1; micActive = false; },
    });
    const {diagnosticRecordStart: start, diagnosticRecordClear: clear,
        diagnosticRecordingPlayer: player, diagnosticRecordingStatus: status} = elements;
    await start.emit('click');
    assert.match(status.textContent, /先连接/);
    assert.equal(timer, undefined);
    micActive = true;
    await start.emit('click');
    ui.push(new Int16Array(320).buffer);
    timer();
    assert.equal(player.src, 'blob:test-preview');
    assert.equal(player.hidden, false);
    assert.equal(timer, null);
    await player.emit('play');
    await player.emit('play');
    assert.equal(playbackStops, 2);
    await clear.emit('click');
    assert.equal(player.hidden, true);
    assert.equal(player.src, undefined);
    assert.deepEqual(revoked, ['blob:test-preview']);
    micActive = true;
    await start.emit('click');
    ui.push(new Int16Array(320).buffer);
    windowHandlers.get('pagehide')();
    ui.finish();
    assert.equal(player.hidden, true);
    assert.equal(timer, null);
});
