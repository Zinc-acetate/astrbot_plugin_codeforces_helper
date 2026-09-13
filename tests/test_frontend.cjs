const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

process.env.TZ = 'UTC';
const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const settle = () => new Promise(resolve => setImmediate(resolve));

function consoleFixture() {
    const elements = new Map();
    const get = id => {
        if (!elements.has(id)) {
            const classes = new Set();
            elements.set(id, {
                value: '', textContent: '', innerHTML: '',
                addEventListener() {},
                classList: {
                    add: name => classes.add(name),
                    remove: name => classes.delete(name),
                    contains: name => classes.has(name),
                    toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
                },
            });
        }
        return elements.get(id);
    };
    get('board-mode').value = 'solved_7';
    const requests = [];
    const context = vm.createContext({
        document: {
            getElementById: get,
            documentElement: {dataset: {}},
            querySelector: () => ({}),
            querySelectorAll: () => [],
            addEventListener() {},
        },
        localStorage: {getItem: () => null, setItem() {}},
        matchMedia: () => ({matches: false}),
        fetch: url => new Promise((resolve, reject) => requests.push({
            url, reject,
            resolve: data => resolve({ok: true, json: async () => data}),
        })),
    });
    for (const script of scripts) vm.runInContext(script, context);
    return {get, requests, context};
}

const member = name => [{name, cf_handle: name, cf_rating: 1900, total_count: 1, cf_count: 1}];

test('an old board response cannot replace the newly selected rating board', async () => {
    const f = consoleFixture();
    f.get('board-mode').value = 'current_rating';
    const latest = f.context.loadBoard();
    f.requests[1].resolve(member('RatingLeader'));
    await latest;
    f.requests[0].resolve(member('WeekLeader'));
    await settle();
    assert.equal(f.get('board-title').textContent, 'Codeforces 当前 Rating');
    assert.match(f.get('leaderboard').innerHTML, /RatingLeader/);
    assert.doesNotMatch(f.get('leaderboard').innerHTML, /WeekLeader/);
    assert.equal(f.get('king').textContent, 'RatingLeader');
});

test('a stale failure cannot clear a successful later board', async () => {
    const f = consoleFixture();
    f.get('board-mode').value = 'max_rating';
    const latest = f.context.loadBoard();
    f.requests[1].resolve(member('MaxLeader'));
    await latest;
    f.requests[0].reject(new Error('old request failed'));
    await settle();
    assert.match(f.get('leaderboard').innerHTML, /MaxLeader/);
    assert.equal(f.get('rating-result').classList.contains('danger'), false);
});

test('refreshing the same mode also ignores an older response', async () => {
    const f = consoleFixture();
    const latest = f.context.loadBoard();
    f.requests[1].resolve(member('FreshLeader'));
    await latest;
    f.requests[0].resolve(member('StaleLeader'));
    await settle();
    assert.match(f.get('leaderboard').innerHTML, /FreshLeader/);
    assert.equal(f.get('king').textContent, 'FreshLeader');
});

test('latest failure stays visible even if an older request later succeeds', async () => {
    const f = consoleFixture();
    f.get('board-mode').value = 'current_rating';
    const latest = f.context.loadBoard();
    f.requests[1].reject(new Error('latest request failed'));
    await latest;
    f.requests[0].resolve(member('StaleLeader'));
    await settle();
    assert.doesNotMatch(f.get('leaderboard').innerHTML, /StaleLeader/);
    assert.equal(f.get('rating-result').classList.contains('danger'), true);
});

test('cached times use Beijing time even in a UTC browser runtime', () => {
    const f = consoleFixture();
    assert.match(f.context.fmtTime(1789300800), /20:00:00/);
    f.requests[0].resolve([]);
});
