import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

test('restores user-only inputs without inventing empty assistant bubbles', () => {
    const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
    const start = source.indexOf('function renderDialogueSnapshot(items) {');
    const end = source.indexOf('\nfunction disconnect()', start);
    assert.ok(start >= 0 && end > start);
    const bubbles = [];
    const context = {
        messageList: {replaceChildren() { bubbles.length = 0; }},
        messagesByTurn: new Map(), turnStartedAt: new Map(), turnTelemetry: new Map(),
        voiceTimings: new Map(),
        appendMessage(role, text) {
            const label = {};
            const element = {role, text, label, dataset: {}, querySelector: () => label};
            bubbles.push(element);
            return element;
        },
        setMessageState(element, state) { element.state = state; },
    };
    vm.createContext(context);
    vm.runInContext(source.slice(start, end), context);
    context.renderDialogueSnapshot([
        {turn_id: 'accepted', lane: 'guest', user_text: '我想学英语', assistant_text: '',
            reply_status: 'user_only', speaker: {display_name: 'Guest'}},
        {turn_id: 'completed', lane: 'family', user_text: '练口语', assistant_text: '好',
            reply_status: 'completed', speaker: {display_name: '鱼鱼'}},
    ]);
    assert.deepEqual(bubbles.map(item => [item.role, item.text]), [
        ['user', '我想学英语'], ['user', '练口语'], ['assistant', '好'],
    ]);
    assert.match(bubbles[0].title, /用户输入已保留/);
    assert.equal(bubbles[0].label.textContent, 'Guest');
    assert.equal(bubbles[1].label.textContent, '鱼鱼');
    assert.equal(context.messagesByTurn.has('accepted'), false);
    assert.equal(context.messagesByTurn.get('completed').state, 'completed');
    context.renderDialogueSnapshot([]);
    assert.equal(bubbles.length, 0);
    assert.equal(context.messagesByTurn.size, 0);
});
