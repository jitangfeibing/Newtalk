const OUTCOMES = {running: '进行中', completed: '已完成', cancelled: '已中断', failed: '失败'};
const REASONS = {
    candidate: '候选（不代表最终发送）', input_budget: '完整输入预算不足',
    included: '已选入', character_budget: '达到字符预算',
    older_than_budget_boundary: '早于连续历史的预算边界', window_limit: '超出轮数窗口',
    interrupted: '生成被取消，未进入历史', generation_failed: '本轮失败，未进入历史',
    explicit_guest: '文字输入选择了访客', explicit_member: '文字输入选择了成员',
    matched: '声纹匹配成功', no_match: '声纹未匹配', timeout: '声纹等待超时',
    unavailable: '声纹服务不可用', invalid_audio: '音频不满足识别要求',
    failed: '识别异常', identity_missing: '匹配成员已不可用',
};
const COMMIT_STATES = {
    completed: '已完成', inserted: '已入队', not_inserted: '未新增（关闭或已存在）',
    not_attempted: '未执行', unknown: '结果未知（操作中取消）', failed: '失败',
    pending: '仍在落库', user_only: '仅保留用户输入', not_retained: '已清理或淘汰',
    skipped: '未写入（已清理/成员失效）',
};

function node(tag, className = '', text = '') {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = text;
    return element;
}

function card(title) {
    const element = node('section', 'context-card');
    element.append(node('h3', '', title));
    return element;
}

function facts(values) {
    const list = node('dl', 'context-facts');
    for (const [label, value] of values) list.append(node('dt', '', label), node('dd', '', String(value ?? '未记录')));
    return list;
}

function code(value) {
    return node('pre', 'context-code', typeof value === 'string' ? value : JSON.stringify(value, null, 2));
}

function parseResult(text) {
    try { return JSON.parse(text); } catch { return null; }
}

function timeLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间未记录' : date.toLocaleTimeString('zh-CN', {hour12: false});
}

function latencySample(events) {
    const last = name => events.findLast(event => event.event === name);
    const selected = last('context_selected');
    const playback = last('playback_audible');
    return {
        turn_id: selected?.turn_id || events[0]?.turn_id,
        at: selected?.at, source: selected?.source,
        speaker: selected?.identity?.display_name,
        outcome: last('turn_end')?.outcome || 'running',
        input: last('voice_input_timing'),
        retrieval: events.filter(event => event.event === 'memory_search_result')
            .map(event => {
                const result = parseResult(event.result);
                const completed = events.find(item => item.event === 'memory_search_completed'
                    && item.call_id === event.call_id);
                return {elapsed_ms: event.elapsed_ms ?? completed?.elapsed_ms,
                    status: result?.status, code: result?.code, results: result?.memories?.length};
            }),
        model_rounds: events.filter(event => event.event === 'llm_round_first_token'
            || event.event === 'llm_tool_decision_completed')
            .map(event => ({event: event.event, round: event.round_index, elapsed_ms: event.elapsed_ms})),
        llm_diagnostics: events.filter(event => event.event === 'llm_request_timing'),
        models: [...new Set(events.filter(event => event.event === 'llm_request').map(event => event.request.model))],
        request_count: events.filter(event => event.event === 'llm_request').length,
        mode: last('memory_search_usage')?.mode,
        thinking: last('llm_request')?.request?.thinking?.type || 'default',
        generation: last('turn_latency'),
        playback: playback ? {output_clock: playback.output_clock, metrics: playback.metrics} : null,
    };
}

function latencyLabel(value) {
    return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} ms` : '未记录';
}

export function initializeContextDashboard(showView) {
    const panel = document.querySelector('#context');
    const status = document.querySelector('#contextDashboardStatus');
    const list = document.querySelector('#contextTurnList');
    const count = document.querySelector('#contextTurnCount');
    const detail = document.querySelector('#contextTurnDetail');
    const filter = document.querySelector('#contextTurnFilter');
    const refreshButton = document.querySelector('#contextRefreshButton');
    const autoRefresh = document.querySelector('#contextAutoRefresh');
    const exportButton = document.querySelector('#contextLatencyExport');
    let exportController = null;
    let visible = false;
    let busy = false;
    let generation = 0;
    let controller = null;
    let selectedId = null;
    let turns = [];
    let renderedId = null;
    let fingerprint = '';
    let listFingerprint = '';
    let autoReady = true;

    function notice(state, text) {
        status.dataset.state = state;
        status.textContent = text;
    }

    function empty(title, text) {
        const element = node('div', 'context-empty');
        element.append(node('strong', '', title), node('p', '', text));
        detail.replaceChildren(element);
        renderedId = null;
        fingerprint = '';
    }

    function renderList() {
        const query = filter.value.trim().toLocaleLowerCase();
        const nextFingerprint = JSON.stringify([selectedId, query, turns]);
        if (listFingerprint === nextFingerprint) return;
        listFingerprint = nextFingerprint;
        const matches = turns.filter(turn => `${turn.turn_id} ${turn.preview} ${turn.speaker}`.toLocaleLowerCase().includes(query));
        list.replaceChildren();
        count.textContent = String(turns.length);
        for (const turn of matches) {
            const item = node('li');
            const button = node('button', 'context-turn-button');
            button.type = 'button';
            button.dataset.contextTurn = turn.turn_id;
            button.setAttribute('aria-pressed', String(selectedId === turn.turn_id));
            const meta = node('div', 'context-turn-meta');
            meta.append(node('span', '', `${timeLabel(turn.at)} · ${turn.speaker}`), node('code', '', turn.turn_id.slice(0, 8)));
            const tail = node('div', 'context-turn-meta');
            tail.append(node('span', `context-outcome ${turn.outcome}`, OUTCOMES[turn.outcome] || turn.outcome),
                node('span', '', `${turn.request_count} 次模型请求`));
            button.append(meta, node('strong', 'context-turn-preview', turn.preview), tail);
            item.append(button);
            list.append(item);
        }
        if (!matches.length) list.append(node('li', 'context-list-empty', query ? '没有匹配轮次' : '还没有本进程记录'));
    }

    function renderDetail(events) {
        const nextFingerprint = JSON.stringify(events);
        if (renderedId === selectedId && fingerprint === nextFingerprint) return;
        const retainOpen = renderedId === selectedId;
        const opened = new Set([...detail.querySelectorAll('details[open][data-key]')].map(item => item.dataset.key));
        function disclosure(key, label, defaultOpen = false) {
            const element = node('details', 'context-disclosure');
            element.dataset.key = key;
            element.open = retainOpen ? opened.has(key) : defaultOpen;
            element.append(node('summary', '', label));
            return element;
        }

        const selected = events.find(event => event.event === 'context_selected');
        const ending = [...events].reverse().find(event => event.event === 'turn_end');
        const accepted = events.find(event => event.event === 'user_input_accepted');
        const inputStored = [...events].reverse().find(event => event.event === 'user_input_persistence');
        const requests = events.filter(event => event.event === 'llm_request');
        const usage = [...events].reverse().find(event => event.event === 'memory_search_usage');
        const searches = events.filter(event => event.event === 'memory_search_result');
        const heading = node('header', 'context-selection-heading');
        heading.append(node('p', 'eyebrow', 'TURN SNAPSHOT'), node('code', '', selectedId));
        if (!selected) heading.append(node('p', 'context-warning', '首部记录未捕获或已淘汰，以下只展示仍可确认的片段。'));

        const identity = card('01 / 身份与画像');
        identity.append(facts([
            ['说话人', selected?.identity?.display_name],
            ['来源', selected ? (selected.source === 'voice' ? '语音输入' : '文字输入') : null],
            ['判定', REASONS[selected?.identity?.reason] || selected?.identity?.reason],
            ['匹配分数', selected?.identity?.score == null ? '不适用 / 未记录' : selected.identity.score.toFixed(4)],
            ['画像缓存', selected ? (selected.profile?.ready ? '已就绪' : '未就绪') : null],
            ['本轮画像', selected ? (selected.profile?.injected ? `已注入 · ${selected.profile.chars} 字符` : '未注入') : null],
        ]));

        const history = card('02 / 历史如何取舍');
        const latestBudget = requests.at(-1)?.budget;
        const includedIds = latestBudget ? new Set(latestBudget.included_history_turn_ids) : null;
        const omittedIds = latestBudget ? new Set(latestBudget.omitted_history_turn_ids) : null;
        const included = latestBudget?.included_history_groups
            ?? selected?.history?.filter(item => item.reason === 'included').length;
        history.append(facts([
            ['最近一次请求选入', latestBudget ? `${included} 轮` : '尚无最终预算记录'],
            ['候选缓存上限', selected?.limits ? `${selected.limits.max_turns} 条输入（非数据库保留期限）` : null],
            ['完整输入预算', latestBudget ? `${latestBudget.after_tokens_estimate} / ${latestBudget.input_token_limit} Token（估算）` : null],
        ]));
        const decisions = disclosure('history', '逐条查看取舍原因');
        for (const item of [...(selected?.history || []), ...(selected?.recent_exclusions || [])]) {
            const row = node('p', 'context-history-row');
            const reason = omittedIds?.has(item.turn_id) ? 'input_budget'
                : includedIds?.has(item.turn_id) ? 'included' : item.reason;
            const label = `${REASONS[reason] || reason}${item.reply_status === 'user_only' ? ' · 仅用户输入，无完整助手回答' : ''}`;
            row.append(node('code', '', item.turn_id.slice(0, 8)), node('span', '', label));
            decisions.append(row);
        }
        decisions.append(node('p', 'context-hint', '近期排除原因仅限本连接观察到的事件，不代表全部历史。'));
        history.append(decisions);

        const memory = card('03 / 记忆有没有参与');
        const usageLabel = usage?.mode === 'preload'
            ? (usage.used ? '服务端前置检索 · 模型自行判断是否采用' : usage.skip_reason === 'guest' ? '访客不读取成员记忆' : '记忆未启用，直接回答')
            : usage ? (usage.used ? '模型调用了检索工具' : usage.offered ? '提供了工具，模型未调用' : '本轮没有提供检索工具') : '调用结果尚未记录';
        memory.append(node('p', 'context-emphasis', usageLabel));
        for (const [index, search] of searches.entries()) {
            const result = parseResult(search.result);
            const argumentsValue = parseResult(search.arguments);
            const label = result?.status === 'no_match' ? '本次未命中（不代表事情没发生）'
                : result?.status === 'ok' ? `返回 ${result.memories?.length || 0} 条候选`
                    : result?.status === 'truncated' ? '结果超出预算，已截断'
                        : `查询异常 · ${result?.code || '未记录'}`;
            memory.append(node('p', '', label));
            if (Number.isFinite(search.elapsed_ms)) {
                memory.append(node('p', '', `检索等待：${search.elapsed_ms.toFixed(1)} ms`));
            }
            const resultView = disclosure(`memory-${index}`, argumentsValue?.query || '查看检索参数与结果');
            resultView.append(code({arguments: argumentsValue || search.arguments, result: result || search.result}));
            memory.append(resultView);
        }
        memory.append(node('p', 'context-hint', '未检索、未命中和服务异常是三种不同情况。'));
        if (latestBudget?.tool_results_truncated?.length || latestBudget?.memory_context_truncated) {
            memory.append(node('p', 'context-warning', '检索结果因本轮输入预算被省略；模型实际收到的是 truncated 提示，不是下方完整检索原文。'));
        }

        const top = node('div', 'context-card-grid');
        top.append(identity, history, memory);
        const input = card('04 / 实际交给模型的请求');
        input.append(node('p', 'context-hint', '包含最终系统提示。工具调用后的第二次请求也在这里；准备发出不等于供应商已收到。'));
        if (!requests.length) input.append(node('p', '', '尚未捕获真实模型请求：可能仍在处理中、使用测试后端，或记录已淘汰。'));
        const rejected = [...events].reverse().find(event => event.event === 'context_budget_rejected');
        if (rejected) input.append(node('p', 'context-warning',
            `请求未发送：系统信息和当前输入仍超预算，估算 ${rejected.budget.after_tokens_estimate} / ${rejected.budget.input_token_limit} Token。请缩短输入或调整预算。`));
        for (const [index, event] of requests.entries()) {
            const request = event.request;
            const messages = request.messages || [];
            const round = disclosure(`request-${event.request_index || index + 1}`,
                `第 ${event.request_index || index + 1} 次请求 · ${request.model} · ${messages.length} 条消息`, index === 0);
            round.append(node('p', 'context-hint', `思考模式：${request.thinking?.type === 'disabled' ? '已显式关闭'
                : request.thinking?.type === 'enabled' ? '已显式开启' : '供应商默认（未指定）'}`));
            const diagnostic = events.find(item => item.event === 'llm_request_timing'
                && item.call_id === event.call_id);
            if (diagnostic) {
                round.append(facts([
                    ['本地准备（请求起点→准备完成）', latencyLabel(diagnostic.prepared_ms)],
                    ['SDK 开始→首次 HTTP 发起', latencyLabel(diagnostic.attempts.length
                        ? diagnostic.attempts[0].request_start_ms - diagnostic.sdk_start_ms : null)],
                    ['请求起点→首个流事件', latencyLabel(diagnostic.first_event_ms)],
                    ['请求起点→首个思考片段', latencyLabel(diagnostic.first_reasoning_ms)],
                    ['请求起点→回答首字', latencyLabel(diagnostic.first_text_ms)],
                    ['首个思考片段→回答首字', latencyLabel(Number.isFinite(diagnostic.first_reasoning_ms)
                        && Number.isFinite(diagnostic.first_text_ms)
                        ? diagnostic.first_text_ms - diagnostic.first_reasoning_ms : null)],
                    ['HTTP 尝试次数', diagnostic.attempts.length || '未观测（非零重试证明）'],
                    ['输入规模', `${diagnostic.request_bytes ?? '未记录'} 字节 / ${diagnostic.input_tokens_estimate ?? '未记录'} Token（估算）`],
                    ['请求指纹', diagnostic.payload_sha256],
                ]));
                for (const attempt of diagnostic.attempts) {
                    const phaseDuration = phase => {
                        const start = attempt.phases.find(item => item.phase === phase && item.event === 'started');
                        const end = attempt.phases.findLast(item => item.phase === phase && item.event === 'complete');
                        return start && end ? latencyLabel(end.at_ms - start.at_ms) : '未观测';
                    };
                    const attemptView = disclosure(`request-${index}-attempt-${attempt.index}`,
                        `HTTP 第 ${attempt.index} 次尝试 · 状态 ${attempt.status_code ?? '未收到响应头'}`);
                    attemptView.append(facts([
                        ['SDK 重试计数', attempt.sdk_retry_count ?? '未观测'],
                        ['DNS/TCP 建连', phaseDuration('connect_tcp')],
                        ['TLS 握手', phaseDuration('start_tls')],
                        ['本次 HTTP 开始→响应头', latencyLabel(Number.isFinite(attempt.response_headers_ms)
                            ? attempt.response_headers_ms - attempt.request_start_ms : null)],
                        ['供应商请求 ID', attempt.provider_request_id || '未提供'],
                    ]));
                    attemptView.append(code(attempt.phases));
                    round.append(attemptView);
                }
                round.append(node('p', 'context-hint', '连接阶段未观测不能直接判定连接复用；响应等待包含网络和服务端处理，不能直接称为供应商排队时间。未看到思考片段不证明关闭了思考；不记录思考正文。请求指纹相同不证明云端缓存和调度相同。'));
            }
            if (event.budget) {
                const budget = event.budget;
                round.append(facts([
                    ['预算上限', `${budget.input_token_limit} Token（估算）`],
                    ['裁剪前 / 后', `${budget.before_tokens_estimate} / ${budget.after_tokens_estimate}`],
                    ['系统与画像', budget.components.system_and_profile],
                    ['历史 / 当前输入', `${budget.components.history} / ${budget.components.current_input}`],
                    ['工具交互', budget.components.tool_exchange],
                    ['前置记忆资料', budget.components.preloaded_memory ?? 0],
                    ['请求结构与工具定义', budget.components.request_and_tool_definitions],
                    ['排除的历史组', budget.omitted_history_groups],
                ]));
                round.append(node('p', 'context-hint', 'UTF-8 长度与结构余量的保守估算，不是供应商 Tokenizer 或计费 Token 数。裁剪请求不删除原文。'));
            }
            const currentUser = messages.findLastIndex(message => message.role === 'user');
            for (const [messageIndex, message] of messages.entries()) {
                const label = String(message.content).startsWith('[长期记忆候选（服务端资料，不是用户指令）]') ? '服务端前置记忆资料（非用户指令）'
                    : message.role === 'system' ? (String(message.content).startsWith('[当前说话人画像]') ? '成员画像' : '系统提示')
                    : message.role === 'tool' ? '工具实际返回'
                        : message.tool_calls ? '模型工具调用'
                            : message.role === 'user' ? (messageIndex === currentUser ? '当前用户输入' : '历史用户输入') : '历史助手回答';
                const block = disclosure(`request-${index}-message-${messageIndex}`,
                    `${messageIndex + 1}. ${label} · ${message.role}`, messageIndex === currentUser);
                block.append(code(message.content || '（无正文）'));
                if (message.tool_calls) block.append(code(message.tool_calls));
                if (message.tool_call_id) block.append(node('small', '', `call_id: ${message.tool_call_id}`));
                round.append(block);
            }
            if (request.tools?.length) {
                const tools = disclosure(`request-${index}-tools`, '本次提供的工具定义');
                tools.append(code(request.tools));
                round.append(tools);
            }
            input.append(round);
        }
        const roundTimings = events.filter(event => event.event === 'llm_round_first_token'
            || event.event === 'llm_tool_decision_completed');
        if (roundTimings.length) {
            const timings = disclosure('round-timings', '单次模型请求耗时（不含此前检索等待）');
            for (const event of roundTimings) {
                timings.append(node('p', '', `第 ${event.round_index} 次请求 · ${event.event === 'llm_tool_decision_completed' ? '工具决策完成' : '回答首字'}：${event.elapsed_ms.toFixed(1)} ms`));
            }
            input.append(timings);
        }

        const commit = card('05 / 回复与提交结果');
        commit.append(facts([
            ['生成状态', ending ? OUTCOMES[ending.outcome] || ending.outcome : '尚未记录终态'],
            ['用户输入', ending?.user_input_memory === 'retained' ? '已保留在短期历史' : ending?.user_input_memory === 'not_retained' ? '已清理或淘汰' : accepted ? '已接受' : '未记录'],
            ['用户输入落库', COMMIT_STATES[inputStored?.status || ending?.user_input_database] || '未记录'],
            ['内存上下文', COMMIT_STATES[ending?.dialogue_memory]],
            ['数据库', COMMIT_STATES[ending?.dialogue_database]],
            ['记忆任务队列', COMMIT_STATES[ending?.memory_queue]],
            ['未入队原因', ending?.memory_skip_reason === 'guest' ? '当前是访客' : ending?.memory_skip_reason === 'turn_not_completed' ? '助手回复未完成' : ending?.memory_skip_reason === 'dialogue_not_persisted' ? 'Dialogue 未确认持久化' : '不适用 / 未记录'],
        ]));
        commit.append(node('p', 'context-hint', '入队不等于 MemOS 形成记忆。结果未知不能按失败处理；本面板不修复提交一致性。'));
        const reply = node('div', 'context-reply');
        reply.append(node('h4', '', '实际生成的回答'), code(ending?.response_text || '没有完整回复快照；不代表此前没有输出过部分文本。'));
        reply.append(node('p', 'context-hint', '人工核对：是否承接当前话题，是否把候选记忆当成事实，是否机械播报检索过程。不显示虚构的质量评分。'));
        commit.append(reply);
        const sample = latencySample(events);
        const latency = card('06 / 声音等待时间');
        latency.append(facts([
            ['说完→首个非静音输出（估算）', latencyLabel(sample.playback?.metrics.speech_end_to_audible_ms)],
            ['收到判停→非静音输出', latencyLabel(sample.playback?.metrics.vad_to_audible_ms)],
            ['Turn→非静音输出', latencyLabel(sample.playback?.metrics.turn_to_audible_ms)],
            ['收到 TTS 首帧→非静音输出', latencyLabel(sample.playback?.metrics.tts_to_audible_ms)],
            ['输出计时基准', sample.playback?.output_clock === 'output_timestamp' ? '浏览器设备输出时间戳（估计）'
                : sample.playback ? '浏览器渲染时钟（降级）' : '未收到非静音输出回执'],
            ['VAD 确认尾静音', latencyLabel(sample.input?.vad_silence_ms)],
            ['服务端判停→ASR Final', latencyLabel(sample.input?.vad_to_asr_final_ms)],
            ['服务端判停→身份就绪', latencyLabel(sample.input?.vad_to_speaker_ready_ms)],
            ['ASR 后身份汇合等待', latencyLabel(sample.input?.speaker_join_ms)],
            ['声纹 Provider（与 ASR 并行）', latencyLabel(sample.input?.voiceprint_provider_ms)],
            ['Turn→文本首字', latencyLabel(sample.generation?.first_text_ms)],
            ['Turn→服务端 TTS 首帧', latencyLabel(sample.generation?.first_audio_ms)],
        ]));
        latency.append(node('p', 'context-hint', '词尾是 VAD 最后确认语音的样本位置，不是人工标注；非静音阈值为 -60 dBFS，不保证是首个语音音素。设备时间戳不是麦克风实录的物理出声。ASR 与声纹有并行，耗时不能直接相加；未回报不能当作 0ms。'));
        const raw = disclosure('raw', `原始事件 · ${events.length} 条`);
        raw.append(code(events));
        detail.replaceChildren(heading, top, input, commit, latency, raw);
        renderedId = selectedId;
        fingerprint = nextFingerprint;
    }

    async function get(path, signal) {
        const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', signal});
        if (!response.ok) {
            const error = new Error(response.status === 401 ? '请先创建或恢复家庭空间。'
                : response.status === 404 ? (path.includes('?') ? '上下文接口尚未加载，请重启主服务。'
                    : '该轮记录未捕获、已过期或已淘汰。历史聊天记录不等于调试快照。')
                    : response.status === 503 ? '上下文调试未启用，请检查服务端配置。' : `读取失败 (${response.status})`);
            error.status = response.status;
            throw error;
        }
        return response.json();
    }

    async function refresh(force = false) {
        if (!visible || (busy && !force)) return;
        controller?.abort();
        controller = new AbortController();
        const current = ++generation;
        const signal = controller.signal;
        busy = true;
        refreshButton.disabled = true;
        try {
            const payload = await get('/api/debug/context?limit=50', signal);
            if (current !== generation) return;
            turns = payload.turns;
            if (!payload.enabled) {
                autoReady = false;
                turns = [];
                selectedId = null;
                renderList();
                notice('disabled', '服务端未启用调试：设置 NEWTALK_CONTEXT_DEBUG=true 后重启主服务。');
                empty('尚未启用上下文调试', '此功能默认关闭，开启后才采集私人上下文；不会补录之前的聊天。');
                return;
            }
            autoReady = true;
            if (!selectedId && turns.length) selectedId = turns[0].turn_id;
            renderList();
            notice('ready', `调试已开启 · 当前设备 ${turns.length} 个近期轮次 · 请在验收后关闭并清理敏感日志。`);
            if (!selectedId) {
                empty('调试已开启，还没有记录', '先发一条文字或语音消息，再回到这里查看。快照仅保留当前进程近期数据。');
                return;
            }
            const snapshot = await get(`/api/debug/context/${encodeURIComponent(selectedId)}`, signal);
            if (current === generation) renderDetail(snapshot.events);
        } catch (error) {
            if (error.name === 'AbortError' || current !== generation) return;
            const message = error.status ? error.message : '服务连接失败或返回异常，请确认主服务已启动并重试。';
            notice('error', message);
            empty('无法读取该轮上下文', message);
            if (error.status === 401) {
                selectedId = null;
                turns = [];
                renderList();
            }
        } finally {
            if (current === generation) {
                busy = false;
                refreshButton.disabled = false;
            }
        }
    }

    exportButton?.addEventListener('click', async () => {
        exportController?.abort();
        exportController = new AbortController();
        const signal = exportController.signal;
        exportButton.disabled = true;
        try {
            const recent = await get('/api/debug/context?limit=5', signal);
            const samples = [];
            for (const turn of [...recent.turns].reverse()) {
                const snapshot = await get(`/api/debug/context/${encodeURIComponent(turn.turn_id)}`, signal);
                samples.push(latencySample(snapshot.events));
            }
            if (signal.aborted) return;
            const report = {exported_at: new Date().toISOString(), samples,
                note: '当前设备最近5轮（不是自动划分的实验组）；新对话不冻结长期记忆。需人工确认5轮均属本次测试。词尾及设备出声均为估计，未做同录音/上下文/记忆受控A/B。'};
            const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}));
            const link = document.createElement('a');
            link.href = url;
            link.download = `newtalk-latency-${Date.now()}.json`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            notice('ready', `已导出 ${samples.length} 轮时延（含成员名和轮次ID，请妥善保管）。`);
        } catch (error) {
            if (error.name !== 'AbortError') notice('error', '时延导出失败，请刷新后重试。');
        } finally {
            exportButton.disabled = false;
        }
    });

    document.addEventListener('click', event => {
        const trigger = event.target.closest('[data-context-turn], .context-open-button');
        if (!trigger) return;
        const turnId = trigger.dataset.contextTurn || trigger.closest('[data-turn-id]')?.dataset.turnId;
        if (!turnId) return;
        selectedId = turnId;
        if (visible) refresh(true);
        else showView('context');
    });
    window.addEventListener('newtalk:view-changed', event => {
        visible = event.detail.view === 'context';
        if (visible) refresh(true);
        else {
            controller?.abort();
            generation += 1;
            busy = false;
            refreshButton.disabled = false;
        }
    });
    window.addEventListener('newtalk:members-changed', () => {
        // 家庭凭据可能已恢复到另一设备，不能继续展示上一个家庭的私人快照。
        controller?.abort();
        exportController?.abort();
        generation += 1;
        busy = false;
        selectedId = null;
        autoReady = true;
        turns = [];
        renderList();
        empty('选择一轮对话', '只显示当前家庭的调试快照。');
        if (visible) refresh(true);
    });
    window.addEventListener('pagehide', () => exportController?.abort());
    filter.addEventListener('input', renderList);
    refreshButton.addEventListener('click', () => refresh(true));
    setInterval(() => {
        if (visible && !document.hidden && autoReady && autoRefresh.checked) refresh();
    }, 2000);
    panel.addEventListener('keydown', event => {
        if (event.key === 'Escape') showView('conversation');
    });
}
