const memberSelect = document.querySelector('#memoryMemberSelect');
const profileList = document.querySelector('#profileList');
const profileAddForm = document.querySelector('#profileAddForm');
const profilePathInput = document.querySelector('#profilePathInput');
const profileValueInput = document.querySelector('#profileValueInput');
const profileLockedInput = document.querySelector('#profileLockedInput');
const addProfileFieldButton = document.querySelector('#addProfileFieldButton');
const cancelProfileFieldButton = document.querySelector('#cancelProfileFieldButton');
const memorySearchForm = document.querySelector('#memorySearchForm');
const memoryKindSelect = document.querySelector('#memoryKindSelect');
const memorySearchInput = document.querySelector('#memorySearchInput');
const memoryList = document.querySelector('#memoryList');
const memoryCount = document.querySelector('#memoryCount');
const pagination = document.querySelector('#memoryPagination');
const previousButton = document.querySelector('#memoryPreviousButton');
const nextButton = document.querySelector('#memoryNextButton');
const pageLabel = document.querySelector('#memoryPageLabel');
const feedback = document.querySelector('#memoryFeedback');
const serviceState = document.querySelector('#memoryServiceState');
const serviceTitle = document.querySelector('#memoryServiceTitle');
const serviceMessage = document.querySelector('#memoryServiceMessage');
const retryButton = document.querySelector('#memoryRetryButton');
const memorySearchButton = memorySearchForm.querySelector('button[type="submit"]');

let members = [];
let page = 1;
let pages = 0;

async function api(path, options = {}) {
    const response = await fetch(path, {
        credentials: 'same-origin',
        ...options,
        headers: {
            ...(options.body ? {'Content-Type': 'application/json'} : {}),
            ...options.headers,
        },
    });
    if (response.status === 204) return null;
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
        const message = typeof payload?.detail === 'string' ? payload.detail : `请求失败 (${response.status})`;
        const error = new Error(message);
        error.status = response.status;
        throw error;
    }
    return payload;
}

function selectedIdentityId() {
    return memberSelect.value;
}

function setFeedback(message, state = 'neutral') {
    feedback.textContent = message;
    feedback.dataset.state = state;
}

function setServiceState(state, title, message) {
    serviceState.dataset.state = state;
    serviceTitle.textContent = title;
    serviceMessage.textContent = message;
    retryButton.hidden = !['disabled', 'unavailable', 'error'].includes(state);
}

function setManagementEnabled(enabled) {
    addProfileFieldButton.disabled = !enabled;
    memoryKindSelect.disabled = !enabled;
    memorySearchInput.disabled = !enabled;
    memorySearchButton.disabled = !enabled;
    if (!enabled) profileAddForm.hidden = true;
}

function describeMemoryError(error) {
    const detail = String(error.message || '未知错误');
    if (error.status === 503 && detail.toLowerCase().includes('disabled')) {
        return {
            state: 'disabled',
            title: '长期记忆尚未启用',
            message: '请先在 Newtalk 服务端配置 MemOS，再使用画像与长期记忆。',
        };
    }
    if (error.status === 503) {
        return {
            state: 'unavailable',
            title: '记忆服务暂时不可用',
            message: 'Newtalk 已连接，但 MemOS 当前不可达。普通对话仍可继续。',
        };
    }
    if (error.status === 502) {
        return {
            state: 'error',
            title: '记忆服务返回异常',
            message: `${detail}。请检查 MemOS 配置或服务日志。`,
        };
    }
    return {state: 'error', title: '记忆内容读取失败', message: detail};
}

function emptyState(target, message) {
    const empty = document.createElement('p');
    empty.className = 'memory-empty';
    empty.textContent = message;
    target.replaceChildren(empty);
}

function profileRow(field) {
    const row = document.createElement('form');
    const path = document.createElement('code');
    const value = document.createElement('textarea');
    const lockLabel = document.createElement('label');
    const locked = document.createElement('input');
    const actions = document.createElement('div');
    const save = document.createElement('button');
    const remove = document.createElement('button');

    row.className = 'profile-row';
    row.dataset.locked = field.locked ? 'true' : 'false';
    path.textContent = field.path;
    value.value = field.value;
    value.maxLength = 2000;
    value.rows = 2;
    locked.type = 'checkbox';
    locked.checked = field.locked;
    lockLabel.append(locked, document.createTextNode(' 锁定'));
    actions.className = 'inline-actions';
    save.type = 'submit';
    save.className = 'text-button';
    save.textContent = '保存';
    remove.type = 'button';
    remove.className = 'text-button danger';
    remove.textContent = '删除';
    actions.append(lockLabel, save, remove);
    row.append(path, value, actions);

    row.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
            await updateProfile({path: field.path, value: value.value, locked: locked.checked});
            setFeedback(`已更新画像字段：${field.path}`, 'success');
        } catch (error) {
            setFeedback(error.message, 'error');
        } finally {
            save.disabled = false;
        }
    });
    remove.addEventListener('click', async () => {
        if (!window.confirm(`确认删除画像字段“${field.path}”？`)) return;
        try {
            await updateProfile({path: field.path, remove: true});
            setFeedback(`已删除画像字段：${field.path}`, 'success');
        } catch (error) {
            setFeedback(error.message, 'error');
        }
    });
    return row;
}

function renderProfile(profile) {
    if (!profile.fields.length) {
        emptyState(profileList, '暂时没有稳定画像。可以手工添加，或等待成功对话后由记忆服务形成。');
        return;
    }
    profileList.replaceChildren(...profile.fields.map(profileRow));
}

function memoryCard(memory) {
    const card = document.createElement('article');
    const meta = document.createElement('div');
    const kind = document.createElement('span');
    const date = document.createElement('time');
    const summary = document.createElement('div');
    const heading = document.createElement('strong');
    const preview = document.createElement('p');
    const toggle = document.createElement('button');
    const editor = document.createElement('form');
    const title = document.createElement('input');
    const content = document.createElement('textarea');
    const actions = document.createElement('div');
    const save = document.createElement('button');
    const remove = document.createElement('button');

    card.className = 'memory-card';
    card.dataset.kind = memory.kind;
    meta.className = 'memory-card-meta';
    kind.textContent = {fact: '事实', preference: '偏好', event: '经历'}[memory.kind] || memory.kind;
    const timestamp = memory.updated_at || memory.created_at;
    date.textContent = timestamp ? new Date(timestamp).toLocaleDateString('zh-CN') : '时间未知';
    meta.append(kind, date);
    summary.className = 'memory-card-summary';
    heading.className = 'memory-card-title';
    heading.textContent = memory.title || '未命名记忆';
    preview.className = 'memory-card-preview';
    preview.textContent = memory.content || '暂无内容';
    toggle.type = 'button';
    toggle.className = 'memory-card-toggle';
    toggle.textContent = '查看与编辑';
    toggle.setAttribute('aria-expanded', 'false');
    summary.append(heading, preview, toggle);
    editor.className = 'memory-card-editor';
    editor.hidden = true;
    title.value = memory.title;
    title.maxLength = 500;
    title.placeholder = '记忆标题';
    content.value = memory.content;
    content.maxLength = 8000;
    content.rows = 3;
    actions.className = 'inline-actions';
    save.type = 'submit';
    save.className = 'text-button';
    save.textContent = '保存修改';
    remove.type = 'button';
    remove.className = 'text-button danger';
    remove.textContent = '删除记忆';
    actions.append(save, remove);
    editor.append(title, content, actions);
    card.append(meta, summary, editor);

    toggle.addEventListener('click', () => {
        const willOpen = editor.hidden;
        editor.hidden = !willOpen;
        toggle.setAttribute('aria-expanded', String(willOpen));
        toggle.textContent = willOpen ? '收起' : '查看与编辑';
        if (willOpen) title.focus();
    });

    editor.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
            await api(`/api/members/${selectedIdentityId()}/memories/${memory.memory_id}`, {
                method: 'PATCH',
                body: JSON.stringify({title: title.value, content: content.value}),
            });
            setFeedback('记忆已更新。', 'success');
            await loadMemories();
        } catch (error) {
            setFeedback(error.message, 'error');
        } finally {
            save.disabled = false;
        }
    });
    remove.addEventListener('click', async () => {
        if (!window.confirm('确认永久删除这条长期记忆？')) return;
        try {
            await api(`/api/members/${selectedIdentityId()}/memories/${memory.memory_id}`, {method: 'DELETE'});
            setFeedback('记忆已删除。', 'success');
            await loadMemories();
        } catch (error) {
            setFeedback(error.message, 'error');
        }
    });
    return card;
}

function renderMemories(result) {
    memoryCount.textContent = `${result.total} 条`;
    if (!result.items.length) emptyState(memoryList, '没有找到符合条件的长期记忆。');
    else memoryList.replaceChildren(...result.items.map(memoryCard));
    pages = result.pages;
    page = result.page;
    pagination.hidden = false;
    pageLabel.textContent = `第 ${page} / ${Math.max(pages, 1)} 页`;
    previousButton.disabled = page <= 1;
    nextButton.disabled = page >= pages;
}

async function updateProfile(payload) {
    const profile = await api(`/api/members/${selectedIdentityId()}/profile`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
    });
    renderProfile(profile);
}

async function loadProfile() {
    const profile = await api(`/api/members/${selectedIdentityId()}/profile`);
    renderProfile(profile);
}

async function loadMemories() {
    const params = new URLSearchParams({page: String(page), size: '8', kind: memoryKindSelect.value});
    const query = memorySearchInput.value.trim();
    if (query) params.set('query', query);
    const result = await api(`/api/members/${selectedIdentityId()}/memories?${params}`);
    renderMemories(result);
}

async function loadSelectedMember() {
    if (!selectedIdentityId()) return;
    const member = members.find((item) => item.identity_id === selectedIdentityId());
    setServiceState('loading', '正在同步记忆', `正在读取 ${member?.display_name || '当前成员'} 的画像和长期记忆。`);
    setManagementEnabled(false);
    setFeedback('');
    profileList.setAttribute('aria-busy', 'true');
    memoryList.setAttribute('aria-busy', 'true');
    try {
        await Promise.all([loadProfile(), loadMemories()]);
        setServiceState('ready', '记忆服务已连接', `${member?.display_name || '当前成员'} 的内容已经同步。`);
        setManagementEnabled(true);
        setFeedback('');
    } catch (error) {
        const state = describeMemoryError(error);
        setServiceState(state.state, state.title, state.message);
        setManagementEnabled(false);
        emptyState(profileList, '当前无法读取稳定画像。');
        emptyState(memoryList, '当前无法读取历史记忆。');
        memoryCount.textContent = '0 条';
        pagination.hidden = false;
        pageLabel.textContent = '第 1 / 1 页';
        previousButton.disabled = true;
        nextButton.disabled = true;
        setFeedback('');
    } finally {
        profileList.removeAttribute('aria-busy');
        memoryList.removeAttribute('aria-busy');
    }
}

function updateMembers(nextMembers) {
    const selected = memberSelect.value;
    members = nextMembers;
    memberSelect.replaceChildren();
    for (const member of members) {
        const option = document.createElement('option');
        option.value = member.identity_id;
        option.textContent = member.nickname ? `${member.display_name}（${member.nickname}）` : member.display_name;
        memberSelect.append(option);
    }
    const hasMembers = members.length > 0;
    memberSelect.disabled = !hasMembers;
    setManagementEnabled(false);
    if (!hasMembers) {
        setServiceState('empty', '还没有家庭成员', '请先在“家庭成员”中添加成员，再为其管理画像与长期记忆。');
        emptyState(profileList, '添加家庭成员后，这里会显示稳定画像。');
        emptyState(memoryList, '添加家庭成员后，这里会显示长期记忆。');
        memoryCount.textContent = '0 条';
        pagination.hidden = false;
        pageLabel.textContent = '第 1 / 1 页';
        previousButton.disabled = true;
        nextButton.disabled = true;
        return;
    }
    memberSelect.value = members.some((member) => member.identity_id === selected)
        ? selected
        : members[0].identity_id;
    page = 1;
    loadSelectedMember();
}

export function initializeMemoryCenter() {
    window.addEventListener('newtalk:members-changed', (event) => updateMembers(event.detail.members));
    memberSelect.addEventListener('change', () => {
        page = 1;
        loadSelectedMember();
    });
    retryButton.addEventListener('click', () => {
        page = 1;
        loadSelectedMember();
    });
    addProfileFieldButton.addEventListener('click', () => {
        profileAddForm.hidden = false;
        profilePathInput.focus();
    });
    cancelProfileFieldButton.addEventListener('click', () => {
        profileAddForm.reset();
        profileAddForm.hidden = true;
    });
    profileAddForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
            await updateProfile({
                path: profilePathInput.value,
                value: profileValueInput.value,
                locked: profileLockedInput.checked,
            });
            profileAddForm.reset();
            profileAddForm.hidden = true;
            setFeedback('画像字段已添加。', 'success');
        } catch (error) {
            setFeedback(error.message, 'error');
        }
    });
    memorySearchForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        page = 1;
        await loadMemories().catch((error) => setFeedback(error.message, 'error'));
    });
    memoryKindSelect.addEventListener('change', () => {
        page = 1;
        loadMemories().catch((error) => setFeedback(error.message, 'error'));
    });
    previousButton.addEventListener('click', () => {
        if (page <= 1) return;
        page -= 1;
        loadMemories().catch((error) => setFeedback(error.message, 'error'));
    });
    nextButton.addEventListener('click', () => {
        if (page >= pages) return;
        page += 1;
        loadMemories().catch((error) => setFeedback(error.message, 'error'));
    });
}
