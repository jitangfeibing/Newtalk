const OUTCOMES = {running: '进行中', completed: '已完成', cancelled: '已中断', failed: '失败'};
const REASONS = {
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

export function initializeContextDashboard(showView) {
    const panel = document.querySelector('#context');
    const status = document.querySelector('#contextDashboardStatus');
    const list = document.querySelector('#contextTurnList');
    const count = document.querySelector('#contextTurnCount');
    const detail = document.querySelector('#contextTurnDetail');
    const filter = document.querySelector('#contextTurnFilter');
    const refreshButton = document.querySelector('#contextRefreshButton');
    const autoRefresh = document.querySelector('#contextAutoRefresh');
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
        const included = selected?.history?.filter(item => item.reason === 'included').length;
        history.append(facts([
            ['实际选入', included == null ? null : `${included} 轮`],
            ['窗口上限', selected?.limits ? `${selected.limits.max_turns} 轮 / ${selected.limits.max_chars} 字符` : null],
        ]));
        const decisions = disclosure('history', '逐条查看取舍原因');
        for (const item of [...(selected?.history || []), ...(selected?.recent_exclusions || [])]) {
            const row = node('p', 'context-history-row');
            row.append(node('code', '', item.turn_id.slice(0, 8)), node('span', '', REASONS[item.reason] || item.reason));
            decisions.append(row);
        }
        decisions.append(node('p', 'context-hint', '近期排除原因仅限本连接观察到的事件，不代表全部历史。'));
        history.append(decisions);

        const memory = card('03 / 记忆有没有参与');
        const usageLabel = usage ? (usage.used ? '本轮调用了检索' : usage.offered ? '提供了工具，模型未调用' : '本轮没有提供检索工具') : '调用结果尚未记录';
        memory.append(node('p', 'context-emphasis', usageLabel));
        for (const [index, search] of searches.entries()) {
            const result = parseResult(search.result);
            const argumentsValue = parseResult(search.arguments);
            const label = result?.status === 'no_match' ? '本次未命中（不代表事情没发生）'
                : result?.status === 'ok' ? `返回 ${result.memories?.length || 0} 条候选`
                    : result?.status === 'truncated' ? '结果超出预算，已截断'
                        : `查询异常 · ${result?.code || '未记录'}`;
            memory.append(node('p', '', label));
            const resultView = disclosure(`memory-${index}`, argumentsValue?.query || '查看检索参数与结果');
            resultView.append(code({arguments: argumentsValue || search.arguments, result: result || search.result}));
            memory.append(resultView);
        }
        memory.append(node('p', 'context-hint', '未检索、未命中和服务异常是三种不同情况。'));

        const top = node('div', 'context-card-grid');
        top.append(identity, history, memory);
        const input = card('04 / 实际交给模型的请求');
        input.append(node('p', 'context-hint', '包含最终系统提示。工具调用后的第二次请求也在这里；准备发出不等于供应商已收到。'));
        if (!requests.length) input.append(node('p', '', '尚未捕获真实模型请求：可能仍在处理中、使用测试后端，或记录已淘汰。'));
        for (const [index, event] of requests.entries()) {
            const request = event.request;
            const messages = request.messages || [];
            const round = disclosure(`request-${event.request_index || index + 1}`,
                `第 ${event.request_index || index + 1} 次请求 · ${request.model} · ${messages.length} 条消息`, index === 0);
            const currentUser = messages.findLastIndex(message => message.role === 'user');
            for (const [messageIndex, message] of messages.entries()) {
                const label = message.role === 'system' ? (String(message.content).startsWith('[当前说话人画像]') ? '成员画像' : '系统提示')
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

        const commit = card('05 / 回复与提交结果');
        commit.append(facts([
            ['生成状态', ending ? OUTCOMES[ending.outcome] || ending.outcome : '尚未记录终态'],
            ['内存上下文', COMMIT_STATES[ending?.dialogue_memory]],
            ['数据库', COMMIT_STATES[ending?.dialogue_database]],
            ['记忆任务队列', COMMIT_STATES[ending?.memory_queue]],
            ['未入队原因', ending?.memory_skip_reason === 'guest' ? '当前是访客' : ending?.memory_skip_reason === 'dialogue_not_persisted' ? 'Dialogue 未确认持久化' : '不适用 / 未记录'],
        ]));
        commit.append(node('p', 'context-hint', '入队不等于 MemOS 形成记忆。结果未知不能按失败处理；本面板不修复提交一致性。'));
        const reply = node('div', 'context-reply');
        reply.append(node('h4', '', '实际生成的回答'), code(ending?.response_text || '没有完整回复快照；不代表此前没有输出过部分文本。'));
        reply.append(node('p', 'context-hint', '人工核对：是否承接当前话题，是否把候选记忆当成事实，是否机械播报检索过程。不显示虚构的质量评分。'));
        commit.append(reply);
        const raw = disclosure('raw', `原始事件 · ${events.length} 条`);
        raw.append(code(events));
        detail.replaceChildren(heading, top, input, commit, raw);
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
        generation += 1;
        busy = false;
        selectedId = null;
        autoReady = true;
        turns = [];
        renderList();
        empty('选择一轮对话', '只显示当前家庭的调试快照。');
        if (visible) refresh(true);
    });
    filter.addEventListener('input', renderList);
    refreshButton.addEventListener('click', () => refresh(true));
    setInterval(() => {
        if (visible && !document.hidden && autoReady && autoRefresh.checked) refresh();
    }, 2000);
    panel.addEventListener('keydown', event => {
        if (event.key === 'Escape') showView('conversation');
    });
}
