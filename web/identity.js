const loading = document.querySelector('#identityLoading');
const onboarding = document.querySelector('#deviceOnboarding');
const workspace = document.querySelector('#deviceWorkspace');
const deviceIdLabel = document.querySelector('#deviceIdLabel');
const createDeviceButton = document.querySelector('#createDeviceButton');
const recoveryForm = document.querySelector('#recoveryForm');
const recoveryCodeInput = document.querySelector('#recoveryCodeInput');
const recoveryNotice = document.querySelector('#recoveryNotice');
const recoveryCodeLabel = document.querySelector('#recoveryCodeLabel');
const copyRecoveryButton = document.querySelector('#copyRecoveryButton');
const rotateRecoveryButton = document.querySelector('#rotateRecoveryButton');
const memberForm = document.querySelector('#memberForm');
const memberFormTitle = document.querySelector('#memberFormTitle');
const displayNameInput = document.querySelector('#displayNameInput');
const nicknameInput = document.querySelector('#nicknameInput');
const relationshipInput = document.querySelector('#relationshipInput');
const avatarInput = document.querySelector('#avatarInput');
const saveMemberButton = document.querySelector('#saveMemberButton');
const cancelMemberButton = document.querySelector('#cancelMemberButton');
const memberList = document.querySelector('#memberList');
const identityFeedback = document.querySelector('#identityFeedback');

let editingIdentityId = null;
let deviceReadyCallback = null;

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

function setFeedback(message, state = 'neutral') {
    identityFeedback.textContent = message;
    identityFeedback.dataset.state = state;
}

function showRecoveryCode(code) {
    recoveryCodeLabel.textContent = code;
    recoveryNotice.hidden = false;
}

async function activateDevice(device) {
    loading.hidden = true;
    onboarding.hidden = true;
    workspace.hidden = false;
    deviceIdLabel.textContent = device.device_id;
    if (device.recovery_code) showRecoveryCode(device.recovery_code);
    await loadMembers();
    deviceReadyCallback?.(device);
}

function showOnboarding() {
    loading.hidden = true;
    workspace.hidden = true;
    onboarding.hidden = false;
    deviceIdLabel.textContent = '等待创建或恢复';
}

function resetMemberForm() {
    editingIdentityId = null;
    memberForm.reset();
    memberFormTitle.textContent = '添加家庭成员';
    saveMemberButton.textContent = '添加成员';
    cancelMemberButton.hidden = true;
}

function beginEdit(member) {
    editingIdentityId = member.identity_id;
    displayNameInput.value = member.display_name;
    nicknameInput.value = member.nickname || '';
    relationshipInput.value = member.relationship || '';
    avatarInput.value = member.avatar || '';
    memberFormTitle.textContent = `编辑 ${member.display_name}`;
    saveMemberButton.textContent = '保存修改';
    cancelMemberButton.hidden = false;
    displayNameInput.focus();
}

function renderMembers(members) {
    memberList.replaceChildren();
    if (!members.length) {
        const empty = document.createElement('li');
        empty.className = 'member-empty';
        empty.textContent = '还没有成员。先录入姓名，声纹将在 P7.2 接入。';
        memberList.append(empty);
        return;
    }

    for (const member of members) {
        const item = document.createElement('li');
        const avatar = document.createElement('span');
        const details = document.createElement('div');
        const title = document.createElement('strong');
        const metadata = document.createElement('p');
        const actions = document.createElement('div');
        const editButton = document.createElement('button');
        const deleteButton = document.createElement('button');

        item.className = 'member-card';
        avatar.className = 'member-avatar';
        avatar.textContent = member.display_name.slice(0, 1).toUpperCase();
        title.textContent = member.display_name;
        metadata.textContent = [member.nickname, member.relationship].filter(Boolean).join(' · ') || '正式家庭成员';
        details.append(title, metadata);
        actions.className = 'member-actions';
        editButton.type = 'button';
        editButton.className = 'text-button';
        editButton.textContent = '编辑';
        editButton.addEventListener('click', () => beginEdit(member));
        deleteButton.type = 'button';
        deleteButton.className = 'text-button danger';
        deleteButton.textContent = '删除';
        deleteButton.addEventListener('click', async () => {
            if (!window.confirm(`确认删除成员“${member.display_name}”？P7.1 当前只包含本地成员资料。`)) return;
            try {
                await api(`/api/members/${member.identity_id}`, {method: 'DELETE'});
                if (editingIdentityId === member.identity_id) resetMemberForm();
                await loadMembers();
                setFeedback(`已删除成员：${member.display_name}`, 'success');
            } catch (error) {
                setFeedback(error.message, 'error');
            }
        });
        actions.append(editButton, deleteButton);
        item.append(avatar, details, actions);
        memberList.append(item);
    }
}

async function loadMembers() {
    const members = await api('/api/members');
    renderMembers(members);
}

createDeviceButton.addEventListener('click', async () => {
    createDeviceButton.disabled = true;
    try {
        const device = await api('/api/device', {method: 'POST'});
        await activateDevice(device);
        setFeedback('家庭空间已创建。请先保存恢复码。', 'success');
    } catch (error) {
        setFeedback(error.message, 'error');
    } finally {
        createDeviceButton.disabled = false;
    }
});

recoveryForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
        const device = await api('/api/device/recover', {
            method: 'POST',
            body: JSON.stringify({recovery_code: recoveryCodeInput.value}),
        });
        recoveryForm.reset();
        await activateDevice(device);
        setFeedback('已恢复原家庭，旧浏览器设备凭据已失效。', 'success');
    } catch (error) {
        setFeedback(error.message, 'error');
    }
});

memberForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = {
        display_name: displayNameInput.value,
        nickname: nicknameInput.value || null,
        relationship: relationshipInput.value || null,
        avatar: avatarInput.value || null,
    };
    try {
        if (editingIdentityId) {
            await api(`/api/members/${editingIdentityId}`, {
                method: 'PATCH',
                body: JSON.stringify(payload),
            });
            setFeedback('成员资料已更新。', 'success');
        } else {
            await api('/api/members', {
                method: 'POST',
                body: JSON.stringify(payload),
            });
            setFeedback('成员已加入当前家庭。', 'success');
        }
        resetMemberForm();
        await loadMembers();
    } catch (error) {
        setFeedback(error.message, 'error');
    }
});

cancelMemberButton.addEventListener('click', resetMemberForm);

rotateRecoveryButton.addEventListener('click', async () => {
    if (!window.confirm('更换后，旧恢复码会立即失效。是否继续？')) return;
    try {
        const device = await api('/api/device/recovery-code', {method: 'POST'});
        showRecoveryCode(device.recovery_code);
        setFeedback('恢复码已更换，请保存新码。', 'success');
    } catch (error) {
        setFeedback(error.message, 'error');
    }
});

copyRecoveryButton.addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(recoveryCodeLabel.textContent);
        setFeedback('恢复码已复制。', 'success');
    } catch {
        setFeedback('浏览器无法自动复制，请手动选择恢复码。', 'error');
    }
});

export async function initializeIdentity(onDeviceReady) {
    deviceReadyCallback = onDeviceReady;
    try {
        const device = await api('/api/device');
        await activateDevice(device);
    } catch (error) {
        if (error.status === 401) {
            showOnboarding();
            return;
        }
        loading.textContent = `设备身份读取失败：${error.message}`;
        loading.dataset.state = 'error';
    }
}
