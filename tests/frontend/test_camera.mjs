import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createCamera} from '../../web/camera.js';

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    return {promise, resolve, reject};
}

function setup(options = {}) {
    const track = new EventTarget();
    track.stops = 0;
    track.stop = () => { track.stops += 1; };
    const stream = {getTracks: () => [track], getVideoTracks: () => [track]};
    const video = {srcObject: null, readyState: 2, videoWidth: 1920, videoHeight: 1080, play: async () => {}};
    const sends = [];
    const states = [];
    let canvasCalls = 0;
    let mediaCalls = 0;
    const canvas = {getContext: () => ({drawImage() {}}), toDataURL: () => 'data:image/jpeg;base64,anBlZw=='};
    const camera = createCamera({video,
        getUserMedia: args => {
            mediaCalls += 1;
            assert.equal(args.audio, false);
            return options.getUserMedia?.(args) || Promise.resolve(stream);
        },
        createCanvas: () => { canvasCalls += 1; return canvas; },
        render: state => states.push(state),
    });
    camera.connect(event => sends.push(event));
    const request = (id = 'capture') => ({type: 'camera_capture', request_id: id, turn_id: 'turn',
        camera_revision: sends.filter(event => event.type === 'camera_state').at(-1)?.revision,
        expires_at: Date.now() + 3000, max_edge: 1280, max_bytes: 512000});
    return {camera, video, track, stream, sends, states, canvas, request,
        get canvasCalls() { return canvasCalls; }, get mediaCalls() { return mediaCalls; }};
}

test('camera is opened only explicitly; preview and ordinary events never upload frames', async () => {
    const s = setup();
    assert.equal(s.mediaCalls, 0);
    s.camera.handle({type: 'text_delta', text: '你好'});
    assert.equal(s.canvasCalls, 0);
    await s.camera.start();
    await s.camera.start();
    assert.equal(s.mediaCalls, 1);
    assert.equal(s.video.srcObject, s.stream);
    assert.equal(s.canvasCalls, 0);
    assert.deepEqual(s.sends.map(event => event.type), ['camera_state', 'camera_state']);
    assert.equal(s.sends.at(-1).opened, true);
});

test('each tool request captures one current frame, deduplicates and leaves preview open', async () => {
    const s = setup();
    await s.camera.start();
    s.camera.handle(s.request('first'));
    s.camera.handle(s.request('first'));
    s.camera.handle(s.request('second'));
    assert.equal(s.canvasCalls, 2);
    const frames = s.sends.filter(event => event.type === 'camera_frame');
    assert.equal(frames.length, 2);
    assert.deepEqual(frames.map(event => event.request_id), ['first', 'second']);
    assert.equal(frames[0].width, 1280);
    assert.equal(frames[0].height, 720);
    assert.equal(frames[0].mime_type, 'image/jpeg');
    assert.equal(s.track.stops, 0);
    assert.equal(s.camera.active, true);
});

test('camera tool cannot open hardware; off and no ready frame return an error', async () => {
    const s = setup();
    s.camera.handle(s.request('off'));
    assert.equal(s.mediaCalls, 0);
    assert.equal(s.sends.at(-1).status, 'error');
    await s.camera.start();
    s.video.readyState = 0;
    s.camera.handle(s.request('waiting'));
    assert.equal(s.canvasCalls, 0);
    assert.equal(s.sends.at(-1).status, 'error');
    assert.equal(s.camera.active, true);
});

test('expired and disconnected capture requests do not upload or draw', async () => {
    const s = setup();
    await s.camera.start();
    s.camera.handle({...s.request(), expires_at: Date.now() - 1});
    s.camera.connect(null);
    s.camera.handle(s.request('after-disconnect'));
    assert.equal(s.canvasCalls, 0);
    assert.equal(s.sends.filter(event => event.type === 'camera_frame').length, 0);
    assert.equal(s.track.stops, 1);
    assert.equal(s.video.srcObject, null);
});

test('late permission resolution after disconnect releases the acquired camera', async () => {
    const permission = deferred();
    const s = setup({getUserMedia: () => permission.promise});
    const started = s.camera.start();
    assert.equal(s.camera.active, true);
    s.camera.connect(null);
    permission.resolve(s.stream);
    await started;
    assert.equal(s.track.stops, 1);
    assert.equal(s.video.srcObject, null);
    assert.equal(s.camera.active, false);
    assert.equal(s.sends.filter(event => event.opened).length, 0);
});

test('closing during permission wait cannot overwrite a later camera preview', async () => {
    const permission = deferred();
    let calls = 0;
    const s = setup({getUserMedia: () => ++calls === 1 ? permission.promise : Promise.resolve(s.stream)});
    const first = s.camera.start();
    s.camera.stop();
    await s.camera.start();
    let oldStops = 0;
    permission.resolve({getTracks: () => [{stop: () => oldStops++}]});
    await first;
    assert.equal(oldStops, 1);
    assert.equal(s.video.srcObject, s.stream);
    assert.equal(s.camera.active, true);
});

test('permission refusal can be retried and does not open or upload', async () => {
    const permission = deferred();
    const s = setup({getUserMedia: () => permission.promise});
    const started = s.camera.start();
    permission.reject({name: 'NotAllowedError'});
    await started;
    assert.equal(s.camera.active, false);
    assert.match(s.states.at(-1).message, /权限/);
    assert.equal(s.canvasCalls, 0);
});

test('physical track ending releases preview and reports camera off', async () => {
    const s = setup();
    await s.camera.start();
    s.track.dispatchEvent(new Event('ended'));
    assert.equal(s.camera.active, false);
    assert.equal(s.video.srcObject, null);
    assert.equal(s.sends.at(-1).opened, false);
});

test('oversized snapshots are downsized locally and rejected without uploading image bytes', async () => {
    const s = setup();
    await s.camera.start();
    let encodes = 0;
    s.canvas.toDataURL = () => { encodes++; return 'data:image/jpeg;base64,' + 'a'.repeat(800000); };
    s.camera.handle(s.request());
    assert.equal(encodes, 2);
    assert.equal(s.canvas.width, 640);
    assert.equal(s.sends.at(-1).status, 'error');
    assert.equal('image_base64' in s.sends.at(-1), false);
});

test('cancelled and superseded status updates cannot overwrite the current analysis', async () => {
    const s = setup();
    await s.camera.start();
    s.camera.handle(s.request('a'));
    s.camera.handle(s.request('b'));
    s.camera.handle({type: 'vision_status', request_id: 'a', status: 'completed'});
    assert.notEqual(s.states.at(-1).message, '本次分析完成');
    s.camera.handle({type: 'turn_cancelled', turn_id: 'turn'});
    const last = s.states.at(-1);
    assert.match(last.message, /取消/);
    s.camera.handle({type: 'vision_status', request_id: 'b', status: 'completed'});
    assert.equal(s.states.at(-1), last);
    assert.equal(s.camera.active, true);
});

test('capture arriving after Turn cancellation or camera reopening is ignored', async () => {
    const s = setup();
    await s.camera.start();
    const old = s.request('old-camera');
    s.camera.stop();
    await s.camera.start();
    s.camera.handle(old);
    s.camera.handle({type: 'turn_cancelled', turn_id: 'turn'});
    s.camera.handle(s.request('old-turn'));
    s.camera.handle({type: 'vision_status', turn_id: 'turn', request_id: 'old-turn', status: 'capturing'});
    assert.equal(s.canvasCalls, 0);
    assert.equal(s.sends.filter(event => event.type === 'camera_frame').length, 0);
    assert.equal(s.camera.active, true);
});
