import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

test('reminder list shows the explicitly promised Beijing time even if browser locale overrides timezone options', () => {
    const source = readFileSync(new URL('../../web/reminders.js', import.meta.url), 'utf8');
    const start = source.indexOf('const formatTime =');
    const end = source.indexOf('\n\nexport function', start);
    const NativeFormatter = Intl.DateTimeFormat;
    const context = vm.createContext({Date, Intl: {DateTimeFormat: function(locale, options) {
        return new NativeFormatter(locale, {...options, timeZone: 'UTC'});
    }}});
    vm.runInContext(source.slice(start, end) + '\nglobalThis.formatTime = formatTime;', context);
    assert.match(context.formatTime('2026-10-25T15:35:00+00:00'), /23:35/);
    assert.match(context.formatTime('2026-10-25T23:35:00+00:00'), /26日.*07:35/);
});
