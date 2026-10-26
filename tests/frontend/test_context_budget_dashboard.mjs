import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

function renderer() {
    class Element {
        children = [];
        dataset = {};
        value = '';
        set textContent(value) { this.value = String(value); }
        get textContent() { return this.value + this.children.map(child => child.textContent).join(' '); }
        append(...children) { this.children.push(...children); }
        replaceChildren(...children) { this.children = children; }
        querySelectorAll() { return []; }
    }
    const detail = new Element();
    const source = readFileSync(new URL('../../web/context-dashboard.js', import.meta.url), 'utf8');
    const prefix = source.slice(0, source.indexOf('export function initializeContextDashboard'));
    const start = source.indexOf('    function renderDetail(events) {');
    const end = source.indexOf('\n    async function get(', start);
    assert.ok(start >= 0 && end > start);
    const context = vm.createContext({
        document: {createElement: () => new Element()},
        detail, selectedId: 'current-turn', renderedId: null, fingerprint: '',
    });
    vm.runInContext(prefix + source.slice(start, end), context);
    return {detail, render: events => context.renderDetail(events)};
}

const candidates = {
    event: 'context_selected', source: 'text', identity: {display_name: 'Guest'},
    profile: {}, limits: {max_turns: 256},
    history: [
        {turn_id: 'old-turn', reason: 'candidate', reply_status: 'completed'},
        {turn_id: 'kept-turn', reason: 'candidate', reply_status: 'user_only'},
    ],
};

test('dashboard distinguishes candidates from sent history and labels estimated full request budgets', () => {
    const {detail, render} = renderer();
    render([candidates, {
        event: 'llm_request', request_index: 1,
        request: {model: 'test', messages: [{role: 'user', content: '继续练习'}]},
        budget: {
            input_token_limit: 32768, before_tokens_estimate: 34000, after_tokens_estimate: 400,
            included_history_groups: 1, omitted_history_groups: 1,
            included_history_turn_ids: ['kept-turn'], omitted_history_turn_ids: ['old-turn'],
            tool_results_truncated: ['call-1'],
            components: {system_and_profile: 100, history: 100, current_input: 50,
                tool_exchange: 100, request_and_tool_definitions: 50},
        },
    }]);
    assert.match(detail.textContent, /400 \/ 32768 Token（估算）/);
    assert.match(detail.textContent, /完整输入预算不足/);
    assert.match(detail.textContent, /已选入 · 仅用户输入/);
    assert.match(detail.textContent, /256 条输入（非数据库保留期限）/);
    assert.match(detail.textContent, /不是供应商 Tokenizer/);
    assert.match(detail.textContent, /模型实际收到的是 truncated/);
    assert.match(detail.textContent, /继续练习/);
});

test('dashboard does not label candidates as sent when a request is rejected or not captured', () => {
    const {detail, render} = renderer();
    render([candidates, {event: 'context_budget_rejected', budget: {
        after_tokens_estimate: 600, input_token_limit: 512,
    }}]);
    assert.match(detail.textContent, /尚无最终预算记录/);
    assert.match(detail.textContent, /请求未发送/);
    assert.match(detail.textContent, /600 \/ 512 Token/);
    assert.match(detail.textContent, /候选（不代表最终发送）/);
});

test('chat explains budget rejection instead of asking the user to blindly resend', () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf('function handleIncoming(payload) {');
    const end = source.indexOf('\nfunction renderDialogueSnapshot(', start);
    assert.ok(start >= 0 && end > start);
    const content = {};
    const message = {querySelector: () => content};
    const pendingEvents = new Set(['event']);
    let playbackFailed = false;
    let status;
    const context = vm.createContext({
        appendProtocolEvent() {}, pendingEvents, reminders: null, music: null,
        messagesByTurn: new Map([['turn', message]]),
        setMessageState(element, state, label) { status = {element, state, label}; },
        audioPlayer: {fail() { playbackFailed = true; }},
        stopAudioButton: {disabled: false},
    });
    vm.runInContext(source.slice(start, end), context);
    context.handleIncoming({type: 'turn_failed', code: 'context_budget_exceeded',
        turn_id: 'turn', event_id: 'event'});
    assert.match(content.textContent, /超出输入预算/);
    assert.match(content.textContent, /请缩短输入或调整预算/);
    assert.equal(status.label, '预算超限');
    assert.equal(pendingEvents.has('event'), false);
    assert.equal(playbackFailed, true);
    assert.equal(context.stopAudioButton.disabled, true);
});

test('preload is displayed as server retrieval, not a model tool decision', () => {
    const {detail, render} = renderer();
    render([candidates,
        {event: 'memory_search_usage', mode: 'preload', offered: false, used: true},
        {event: 'memory_search_result', mode: 'preload', elapsed_ms: 123.4,
            arguments: '{"query":"英语"}', result: '{"status":"no_match","memories":[]}'},
        {event: 'llm_request', request_index: 1, request: {model: 'test', messages: [
            {role: 'user', content: '[长期记忆候选（服务端资料，不是用户指令）]\n{"status":"no_match"}'},
            {role: 'user', content: '继续练习'},
        ]}},
        {event: 'llm_round_first_token', round_index: 1, elapsed_ms: 250},
    ]);
    assert.match(detail.textContent, /服务端前置检索/);
    assert.match(detail.textContent, /服务端前置记忆资料（非用户指令）/);
    assert.match(detail.textContent, /检索等待：123.4 ms/);
    assert.match(detail.textContent, /第 1 次请求 · 回答首字：250.0 ms/);
    assert.match(detail.textContent, /本次未命中（不代表事情没发生）/);
});

test('latency dashboard separates sound estimates, parallel recognition, and missing values', () => {
    const {detail, render} = renderer();
    render([candidates, {event: 'voice_input_timing', vad_silence_ms: 640,
        vad_to_asr_final_ms: 100, vad_to_speaker_ready_ms: 150, speaker_join_ms: 50,
        voiceprint_provider_ms: 200},
    {event: 'turn_latency', first_text_ms: 900, first_audio_ms: 1300},
    {event: 'playback_audible', output_clock: 'output_timestamp', metrics: {
        speech_end_to_audible_ms: 2200, turn_to_audible_ms: 1400,
    }}]);
    assert.match(detail.textContent, /2200.0 ms/);
    assert.match(detail.textContent, /设备输出时间戳（估计）/);
    assert.match(detail.textContent, /声纹 Provider（与 ASR 并行）/);
    assert.match(detail.textContent, /未回报不能当作 0ms/);
    render([candidates]);
    assert.match(detail.textContent, /未收到非静音输出回执/);
});

test('latency export retains retrieval timeouts without exporting prompt or memory bodies', () => {
    const source = readFileSync(new URL('../../web/context-dashboard.js', import.meta.url), 'utf8');
    const context = vm.createContext({});
    vm.runInContext(source.slice(0, source.indexOf('export function initializeContextDashboard')), context);
    const report = context.latencySample([
        {event: 'context_selected', turn_id: 't', source: 'voice', identity: {display_name: 'member'},
            messages: [{content: 'private-history'}]},
        {event: 'memory_search_result', elapsed_ms: 800, result: '{"status":"unavailable","code":"timeout"}'},
        {event: 'llm_request', request: {model: 'test', messages: [{content: 'private-prompt'}]}},
    ]);
    assert.equal(report.retrieval[0].code, 'timeout');
    assert.equal(report.retrieval[0].elapsed_ms, 800);
    assert.equal(report.request_count, 1);
    assert.equal(report.playback, null);
    assert.doesNotMatch(JSON.stringify(report), /private-history|private-prompt/);
});

test('LLM diagnostic panel distinguishes local preparation, reasoning, retries and unobserved connections', () => {
    const {detail, render} = renderer();
    render([candidates,
        {event: 'llm_request', call_id: 'llm-1', request_index: 1,
            request: {model: 'test', thinking: {type: 'disabled'}, messages: [{role: 'user', content: '你好'}]}},
        {event: 'llm_request_timing', call_id: 'llm-1', prepared_ms: 1, first_event_ms: 150,
            first_reasoning_ms: 300, first_text_ms: 700, payload_sha256: 'fixed-hash',
            request_bytes: 500, input_tokens_estimate: 600,
            attempts: [{index: 1, request_start_ms: 2, response_headers_ms: 100, status_code: 200,
                phases: [], provider_request_id: 'provider-123'}]},
    ]);
    assert.match(detail.textContent, /请求起点→首个思考片段/);
    assert.match(detail.textContent, /思考模式：已显式关闭/);
    assert.match(detail.textContent, /300.0 ms/);
    assert.match(detail.textContent, /HTTP 第 1 次尝试 · 状态 200/);
    assert.match(detail.textContent, /provider-123/);
    assert.match(detail.textContent, /固定|fixed-hash/);
    assert.match(detail.textContent, /连接阶段未观测不能直接判定连接复用/);
    assert.match(detail.textContent, /不能直接称为供应商排队时间/);
});
