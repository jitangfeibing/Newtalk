const center = document.querySelector('#memoryCenter');
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
    const card = document.createElement('form');
    const meta = document.createElement('div');
    const kind = document.createElement('span');
    const date = document.createElement('time');
    const title = document.createElement('input');
    const content = document.createElement('textarea');
    const actions = document.createElement('div');
    const save = document.createElement('button');
    const remove = document.createElement('button');

    card.className = 'memory-card';
    meta.className = 'memory-card-meta';
    kind.textContent = {fact: '事实', preference: '偏好', event: '经历'}[memory.kind] || memory.kind;
    date.textContent = memory.updated_at || memory.created_at || '时间未知';
    meta.append(kind, date);
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
    card.append(meta, title, content, actions);

    card.addEventListener('submit', async (event) => {
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
    pagination.hidden = pages <= 1;
    pageLabel.textContent = `${page} / ${Math.max(pages, 1)}`;
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
    const params = new URLSearchParams({page: String(page), size: '20', kind: memoryKindSelect.value});
    const query = memorySearchInput.value.trim();
    if (query) params.set('query', query);
    const result = await api(`/api/members/${selectedIdentityId()}/memories?${params}`);
    renderMemories(result);
}

async function loadSelectedMember() {
    if (!selectedIdentityId()) return;
    setFeedback('正在读取画像和长期记忆...');
    profileList.setAttribute('aria-busy', 'true');
    memoryList.setAttribute('aria-busy', 'true');
    try {
        await Promise.all([loadProfile(), loadMemories()]);
        setFeedback('内容已同步。', 'success');
    } catch (error) {
        const message = error.status === 503
            ? '长期记忆未启用。请在服务端配置 MemOS 后使用 Memory Center。'
            : error.message;
        emptyState(profileList, message);
        emptyState(memoryList, message);
        setFeedback(message, 'error');
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
    center.hidden = members.length === 0;
    if (!members.length) return;
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
