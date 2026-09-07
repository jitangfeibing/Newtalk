import {recordVoiceprintSample} from './voiceprint-recorder.js';

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
const textSpeakerSelect = document.querySelector('#textSpeakerSelect');

let editingIdentityId = null;
let deviceReadyCallback = null;
let enrollment = null;
let currentMembers = [];

async function api(path, options = {}) {
    const response = await fetch(path, {
        credentials: 'same-origin',
        ...options,
        headers: {
            ...(options.body && !(options.body instanceof FormData) ? {'Content-Type': 'application/json'} : {}),
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

function closeEnrollment() {
    enrollment = null;
    document.querySelector('.voiceprint-enrollment')?.remove();
}

function showVoiceprintEnrollment(member, item) {
    closeEnrollment();
    enrollment = {identityId: member.identity_id, samples: []};
    const panel = document.createElement('div');
    const title = document.createElement('strong');
    const guidance = document.createElement('p');
    const progress = document.createElement('div');
    const recordButton = document.createElement('button');
    const uploadButton = document.createElement('button');
    const cancelButton = document.createElement('button');

    panel.className = 'voiceprint-enrollment';
    title.textContent = `录入 ${member.display_name} 的声纹`;
    guidance.textContent = '请在安静环境自然朗读。每段 4 秒，共录制三段不同内容。';
    progress.className = 'voiceprint-progress';
    const steps = [0, 1, 2].map((index) => {
        const step = document.createElement('span');
        step.textContent = `第 ${index + 1} 段`;
        progress.append(step);
        return step;
    });
    recordButton.type = 'button';
    recordButton.className = 'button secondary';
    recordButton.textContent = '录制第 1 段';
    uploadButton.type = 'button';
    uploadButton.className = 'button';
    uploadButton.textContent = '提交三段声纹';
    uploadButton.disabled = true;
    cancelButton.type = 'button';
    cancelButton.className = 'text-button';
    cancelButton.textContent = '取消';

    recordButton.addEventListener('click', async () => {
        const index = enrollment?.samples.length ?? 0;
        if (!enrollment || index >= 3) return;
        recordButton.disabled = true;
        steps[index].dataset.state = 'recording';
        recordButton.textContent = `正在录制第 ${index + 1} 段 0%`;
        try {
            const sample = await recordVoiceprintSample((value) => {
                recordButton.textContent = `正在录制第 ${index + 1} 段 ${Math.round(value * 100)}%`;
            });
            enrollment.samples.push(sample);
            steps[index].dataset.state = 'complete';
            const next = enrollment.samples.length + 1;
            recordButton.textContent = next <= 3 ? `录制第 ${next} 段` : '三段录音已完成';
            recordButton.disabled = enrollment.samples.length >= 3;
            uploadButton.disabled = enrollment.samples.length !== 3;
        } catch (error) {
            steps[index].dataset.state = 'error';
            recordButton.textContent = `重新录制第 ${index + 1} 段`;
            recordButton.disabled = false;
            setFeedback(error.message, 'error');
        }
    });

    uploadButton.addEventListener('click', async () => {
        if (!enrollment || enrollment.samples.length !== 3) return;
        uploadButton.disabled = true;
        const form = new FormData();
        enrollment.samples.forEach((sample, index) => {
            form.append('samples', sample, `voiceprint-${index + 1}.wav`);
        });
        try {
            await api(`/api/members/${member.identity_id}/voiceprint`, {
                method: 'POST',
                body: form,
            });
            closeEnrollment();
            await loadMembers();
            setFeedback(`${member.display_name} 的声纹已录入。`, 'success');
        } catch (error) {
            uploadButton.disabled = false;
            setFeedback(error.message, 'error');
        }
    });
    cancelButton.addEventListener('click', closeEnrollment);
    const actions = document.createElement('div');
    actions.className = 'voiceprint-actions';
    actions.append(recordButton, uploadButton, cancelButton);
    panel.append(title, guidance, progress, actions);
    item.append(panel);
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
        const voiceprintButton = document.createElement('button');

        item.className = 'member-card';
        avatar.className = 'member-avatar';
        avatar.textContent = member.display_name.slice(0, 1).toUpperCase();
        title.textContent = member.display_name;
        const memberMetadata = [member.nickname, member.relationship].filter(Boolean).join(' · ') || '正式家庭成员';
        metadata.textContent = `${memberMetadata} · ${member.voiceprint_enrolled ? '声纹已录入' : '未录入声纹'}`;
        details.append(title, metadata);
        actions.className = 'member-actions';
        editButton.type = 'button';
        editButton.className = 'text-button';
        editButton.textContent = '编辑';
        editButton.addEventListener('click', () => beginEdit(member));
        voiceprintButton.type = 'button';
        voiceprintButton.className = 'text-button';
        voiceprintButton.textContent = member.voiceprint_enrolled ? '重新录入' : '录入声纹';
        voiceprintButton.addEventListener('click', () => showVoiceprintEnrollment(member, item));
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
        actions.append(editButton, voiceprintButton);
        if (member.voiceprint_enrolled) {
            const removeVoiceprintButton = document.createElement('button');
            removeVoiceprintButton.type = 'button';
            removeVoiceprintButton.className = 'text-button danger';
            removeVoiceprintButton.textContent = '删除声纹';
            removeVoiceprintButton.addEventListener('click', async () => {
                if (!window.confirm(`确认删除“${member.display_name}”的声纹模板？成员资料会保留。`)) return;
                try {
                    await api(`/api/members/${member.identity_id}/voiceprint`, {method: 'DELETE'});
                    closeEnrollment();
                    await loadMembers();
                    setFeedback(`${member.display_name} 的声纹已删除。`, 'success');
                } catch (error) {
                    setFeedback(error.message, 'error');
                }
            });
            actions.append(removeVoiceprintButton);
        }
        actions.append(deleteButton);
        item.append(avatar, details, actions);
        memberList.append(item);
    }
}

function renderSpeakerOptions(members) {
    const selected = textSpeakerSelect.value;
    textSpeakerSelect.replaceChildren();
    const guest = document.createElement('option');
    guest.value = '';
    guest.textContent = 'Guest（访客）';
    textSpeakerSelect.append(guest);
    for (const member of members) {
        const option = document.createElement('option');
        option.value = member.identity_id;
        option.textContent = member.nickname
            ? `${member.display_name}（${member.nickname}）`
            : member.display_name;
        textSpeakerSelect.append(option);
    }
    textSpeakerSelect.value = members.some((member) => member.identity_id === selected)
        ? selected
        : '';
}

async function loadMembers() {
    closeEnrollment();
    const members = await api('/api/members');
    currentMembers = members;
    renderMembers(members);
    renderSpeakerOptions(members);
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

export function selectedTextSpeaker() {
    return currentMembers.find((member) => member.identity_id === textSpeakerSelect.value) ?? null;
}
