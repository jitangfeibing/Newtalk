import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {createMediaPlayer} from '../../web/media-player.js';

class Audio extends EventTarget {
    playCalls = 0; pauseCalls = 0; src = ''; currentSrc = '';
    play() { this.playCalls += 1; this.currentSrc = this.src; return Promise.resolve(); }
    pause() { this.pauseCalls += 1; }
    removeAttribute() { this.src = ''; this.currentSrc = ''; }
    load() {}
}
const track = {title: '测试曲', artist: '歌手', stream_url: '/api/music/tracks/11111111-1111-1111-1111-111111111111/audio'};
const command = (id, action = 'play') => ({request_id: id, action, track});

test('functional-page media commands keep an ongoing chat busy until the chat ends', async () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf("    if (payload.type === 'media_control' || payload.type === 'media_error' || payload.type === 'media_status') {");
    const end = source.indexOf("    if (payload.type === 'device_control')", start);
    const {player, audio, results} = setup();
    player.setBusy('generation', true);
    const context = vm.createContext({
        payload: {type: 'media_control', turn_id: null, ...command('page-request')},
        reminders: {setBusy() {}}, music: {setBusy: player.setBusy, handle: player.handle},
        messagesByTurn: new Map(),
    });
    vm.runInContext(`(() => {${source.slice(start, end)}})()`, context);
    await flush();
    assert.equal(audio.playCalls, 0);
    assert.equal(results.at(-1).status, 'waiting');
    player.setBusy('generation', false);
    await flush();
    assert.equal(audio.playCalls, 1);
});
async function flush() { for (let i = 0; i < 8; i += 1) await Promise.resolve(); }
function setup() {
    const audio = new Audio(), results = [], states = [];
    const player = createMediaPlayer({audio, report: event => results.push(event), onChange: state => states.push(state)});
    player.connect(true);
    return {player, audio, results, states};
}

test('music is marked playing only after actual browser feedback and duplicate commands do not restart it', async () => {
    const {player, audio, results, states} = setup();
    player.handle(command('r')); player.handle(command('r')); await flush();
    assert.equal(audio.playCalls, 1); assert.equal(results.length, 0);
    assert.equal(states.at(-1).status, 'loading');
    audio.dispatchEvent(new Event('playing'));
    assert.equal(results[0].status, 'playing');
    audio.dispatchEvent(new Event('ended')); assert.equal(states.at(-1).status, 'ended');
});

test('chat and reminders pause music and do not automatically resume it', async () => {
    const {player, audio, states} = setup(); player.handle(command('r')); await flush();
    audio.dispatchEvent(new Event('playing'));
    player.setBusy('reminder', true); player.setBusy('generation', true);
    assert.equal(states.at(-1).status, 'paused');
    player.setBusy('reminder', false); player.setBusy('generation', false); await flush();
    assert.equal(audio.playCalls, 1);
    player.handle(command('resume', 'resume')); await flush(); assert.equal(audio.playCalls, 2);
});

test('explicit play during an active reminder waits until all foreground audio ends', async () => {
    const {player, audio, results} = setup();
    player.setBusy('reminder', true); player.setBusy('chat-audio', true);
    player.handle(command('r')); await flush(); assert.equal(audio.playCalls, 0);
    assert.equal(results[0].status, 'waiting');
    player.setBusy('reminder', false); await flush(); assert.equal(audio.playCalls, 0);
    player.setBusy('chat-audio', false); await flush(); assert.equal(audio.playCalls, 1);
});

test('late play rejection from an old connection cannot affect the new track', async () => {
    const {player, audio, states, results} = setup(); let reject;
    audio.play = () => new Promise((resolve, fail) => { reject = fail; });
    player.handle(command('old')); await flush();
    player.connect(false); player.connect(true);
    audio.play = () => { audio.currentSrc = audio.src; return Promise.resolve(); };
    player.handle(command('new')); await flush(); audio.dispatchEvent(new Event('playing'));
    reject(Error('late failure')); await flush();
    assert.equal(states.at(-1).status, 'playing');
    assert.equal(results.some(item => item.request_id === 'new' && item.status === 'failed'), false);
});

test('browser autoplay failure is reported honestly and can be explicitly retried', async () => {
    const {player, audio, results, states} = setup();
    audio.play = () => Promise.reject(Error('NotAllowedError'));
    player.handle(command('r')); await flush();
    assert.equal(states.at(-1).status, 'failed'); assert.equal(results[0].status, 'failed');
    audio.play = () => { audio.currentSrc = audio.src; return Promise.resolve(); };
    player.handle(command('retry', 'resume')); await flush(); audio.dispatchEvent(new Event('playing'));
    assert.equal(results.at(-1).status, 'playing');
});

test('stop releases audio, no-track resume is explicit, and arbitrary external URLs are rejected', async () => {
    const {player, audio, results} = setup();
    player.handle(command('none', 'resume')); assert.equal(results.at(-1).status, 'no_track');
    player.handle({...command('bad'), track: {...track, stream_url: 'https://evil.test/audio'}});
    await flush(); assert.equal(audio.playCalls, 0);
    player.handle(command('ok')); await flush();
    player.handle(command('stop', 'stop')); assert.equal(audio.src, '');
    assert.equal(results.at(-1).status, 'stopped');
});

test('resolved resources stay silent when explicitly paused and resume without a new lookup', async () => {
    const {player, audio, results, states} = setup();
    player.handle({...command('ready'), revision: 1, autoplay: false}); await flush();
    assert.equal(audio.playCalls, 0);
    assert.equal(results.at(-1).status, 'paused');
    assert.equal(states.at(-1).track.title, track.title);
    player.handle({...command('resume', 'resume'), revision: 1}); await flush();
    assert.equal(audio.playCalls, 1);
});

test('a new unstarted play survives foreground interruption and starts after it ends', async () => {
    const {player, audio, results} = setup();
    player.handle(command('new')); await flush();
    player.setBusy('user', true);
    assert.equal(results.at(-1).status, 'waiting');
    player.setBusy('generation', true); player.setBusy('user', false); await flush();
    assert.equal(audio.playCalls, 1);
    player.setBusy('generation', false); await flush();
    assert.equal(audio.playCalls, 2);
    audio.dispatchEvent(new Event('playing'));
    assert.equal(results.at(-1).status, 'playing');
});

test('automatic pause and natural end are reported after the command acknowledgement', async () => {
    const {player, audio, results} = setup();
    player.handle(command('play')); await flush(); audio.dispatchEvent(new Event('playing'));
    player.setBusy('user', true);
    assert.ok(results.some(event => event.type === 'media_state' && event.status === 'paused'));
    player.setBusy('user', false); player.handle(command('resume', 'resume')); await flush();
    audio.dispatchEvent(new Event('playing')); audio.dispatchEvent(new Event('ended'));
    assert.deepEqual(results.at(-1), {type: 'media_state', request_id: 'resume', action: 'resume', status: 'ended'});
});

test('new lookup revokes an older unstarted play and late controls cannot revive it', async () => {
    const {player, audio, states} = setup();
    player.setBusy('generation', true);
    player.handle({...command('old'), revision: 1});
    player.operation({revision: 2, operation_state: 'resolving', target: '新曲目', desired_playing: true});
    player.setBusy('generation', false); await flush();
    assert.equal(audio.playCalls, 0);
    assert.equal(states.at(-1).target, '新曲目');
    player.handle({...command('stale'), revision: 1}); await flush();
    assert.equal(audio.playCalls, 0);
});

test('late source errors cannot pause the player of a newer operation', async () => {
    const {player, audio, states} = setup();
    player.handle({...command('new'), revision: 2}); await flush();
    audio.dispatchEvent(new Event('playing'));
    player.error('旧请求失败', 1);
    assert.equal(states.at(-1).status, 'playing');
});

test('text submission reserves the foreground before asynchronous audio preparation', async () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf("chatForm.addEventListener('submit', async (event) => {");
    const end = source.indexOf('    const payload = {', start);
    const {player, audio} = setup();
    let prepared;
    const preparing = new Promise(resolve => { prepared = resolve; });
    const context = vm.createContext({
        event: {preventDefault() {}}, messageInput: {value: '聊聊这首歌'},
        socket: {readyState: 1}, WebSocket: {OPEN: 1}, music: player,
        audioPlayer: {prepare: () => preparing}, advertisedAudioFormat: {},
    });
    const body = source.slice(start, end).split('\n').slice(1).join('\n');
    const submitting = vm.runInContext(`(async () => {${body}})()`, context);
    player.handle(command('ready-during-text-submit')); await flush();
    const callsBeforePrepared = audio.playCalls;
    prepared(); await submitting;
    assert.equal(callsBeforePrepared, 0);
});

test('podcast audio shares foreground coordination and pauses like music', async () => {
    const {player, audio, results, states} = setup();
    const episode = {...track, media_type: 'podcast', album: '测试专辑',
        stream_url: track.stream_url.replace('/music/', '/podcasts/')};
    player.setBusy('generation', true);
    player.handle({...command('episode'), track: episode}); await flush();
    assert.equal(audio.playCalls, 0);
    player.setBusy('generation', false); await flush();
    audio.dispatchEvent(new Event('playing'));
    assert.equal(results.at(-1).status, 'playing');
    player.setBusy('user', true); player.setBusy('user', false); await flush();
    assert.equal(states.at(-1).status, 'paused');
    assert.equal(audio.playCalls, 1);
});

test('VAD during playback-only listening does not pause media or stop foreground audio', async () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf("    if (payload.type === 'vad_speech_start') {");
    const end = source.indexOf("    if (payload.type === 'vad_speech_end')", start);
    const {player, audio, states} = setup();
    player.handle(command('play')); await flush(); audio.dispatchEvent(new Event('playing'));
    let stopped = 0;
    const context = vm.createContext({
        payload: {type: 'vad_speech_start', utterance_id: 'background', media_only: true},
        reminders: {setBusy() {}}, music: player, audioPlayer: {stop() { stopped += 1; }},
        ensureVoiceTiming: () => ({}), performance, microphone: {setStatus() {}},
        voiceVadStatus: {}, voiceRecognitionStatus: {},
    });
    vm.runInContext(`(() => {${source.slice(start, end)}})()`, context);
    assert.equal(states.at(-1).status, 'playing');
    assert.equal(stopped, 0);
});

test('ignored playback speech clears timing and busy flags without adding a chat message', () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf("    if (payload.type === 'asr_final') {");
    const end = source.indexOf("    if (payload.type === 'speaker_resolved')", start);
    const busy = [], timings = new Map([['background', {}]]);
    const context = vm.createContext({
        payload: {type: 'asr_final', utterance_id: 'background', text: '', disposition: 'ignored_media'},
        reminders: {setBusy: (...args) => busy.push(args)}, music: {setBusy: (...args) => busy.push(args)},
        voiceTimings: timings, appendMessage() { throw Error('background speech entered chat'); },
        microphone: {setStatus() {}}, voiceRecognitionStatus: {},
    });
    vm.runInContext(`(() => {${source.slice(start, end)}})()`, context);
    assert.equal(timings.size, 0);
    assert.deepEqual(busy, [['user', false], ['user', false]]);
});
