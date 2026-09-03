/**
 * test_worker.mjs — The licence service and the feedback relay, without
 * Cloudflare (#517).
 *
 * Two Workers, no test harness: this imports both modules the way the
 * runtime does and drives their exported helpers and their fetch handlers
 * with a fake D1 that records every statement and answers from a table of
 * canned rows. Nothing here talks to the network — fetch is stubbed where a
 * handler would call out.
 *
 * What it can prove: parsing and escaping helpers, the refusals (missing
 * secrets, bad bodies, wrong password, prototype-named report types), which
 * limiter an endpoint uses, and which SQL a request causes. What it cannot:
 * that the SQL runs against the real schema — that lives in a
 * @cloudflare/vitest-pool-workers suite with a local D1 seeded from the
 * schema files in order, which is the next step and is described in
 * README.md.
 *
 *     node relay/admin/test_worker.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import admin, { clean, isoDate, isEmail, csvCell, parseCursor, machineOf, readBody, makeSession, timingSafeEqual } from './worker.js';
import relay, { labelsFor } from '../worker.js';

// ---------------------------------------------------------------- a fake D1
// prepare(sql).bind(...).first()/all()/run() and batch([...]). Answers come
// from `answers`: the first key found in the SQL text names the rows.
function fakeDB(answers = {}) {
  const log = [];
  const answer = sql => {
    for (const [needle, rows] of Object.entries(answers)) if (sql.includes(needle)) return { results: rows, meta: { changes: rows.length } };
    return { results: [], meta: { changes: 0 } };
  };
  const stmt = (sql, args) => ({
    _sql: sql, _args: args,
    bind: (...a) => stmt(sql, a),
    first: async () => { log.push({ sql, args }); return answer(sql).results[0] ?? null; },
    all: async () => { log.push({ sql, args }); return answer(sql); },
    run: async () => { log.push({ sql, args }); return { meta: { changes: 1 }, results: [] }; },
  });
  return {
    log,
    prepare: sql => stmt(sql, []),
    batch: async stmts => stmts.map(s => { log.push({ sql: s._sql, args: s._args }); return answer(s._sql); }),
    ran: needle => log.some(e => e.sql.includes(needle)),
  };
}
const limiter = ok => ({ limit: async () => ({ success: ok }) });
const req = (path, { method = 'GET', body, headers = {} } = {}) =>
  new Request(`https://admin.example${path}`, { method, body, headers });
async function signingKey() {
  const pair = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
  const der = await crypto.subtle.exportKey('pkcs8', pair.privateKey);
  return btoa(String.fromCharCode(...new Uint8Array(der)));
}
const LICENCE = { id: 'lic-1', kind: 'person', licensee: 'Test', email: 't@example.com', seats: 1, issued: '2026-01-01', expires: '2026-12-31',
                  grace_days: 14, features: '["updates"]', token: 'SM1.a.b', revoked: 0, revoked_reason: '', notes: 'n', created_at: 1, installs: 0 };

// ---------------------------------------------------------------- helpers
test('clean bounds and trims; isoDate accepts a date or nothing', () => {
  assert.equal(clean('  hi  ', 10), 'hi');
  assert.equal(clean('x'.repeat(50), 10).length, 10);
  assert.equal(clean(null, 10), '');
  assert.equal(isoDate(''), '');
  assert.equal(isoDate('2026-09-03'), '2026-09-03');
  assert.throws(() => isoDate('03/09/2026'), /YYYY-MM-DD/);
  assert.throws(() => isoDate('2026-13-45'), /YYYY-MM-DD/);
  assert.ok(isEmail('a@b.co') && !isEmail('a@b') && !isEmail('not an email'));
});

test('csvCell neutralises a formula and doubles quotes (#513)', () => {
  assert.equal(csvCell('=1+1'), '"\'=1+1"');
  assert.equal(csvCell('+5'), '"\'+5"');
  assert.equal(csvCell('-3'), '"\'-3"');
  assert.equal(csvCell('@SUM'), '"\'@SUM"');
  assert.equal(csvCell('\t=x'), '"\'\t=x"');
  assert.equal(csvCell('\r=x'), '"\'\r=x"');
  assert.equal(csvCell('say "hi"'), '"say ""hi"""');
  assert.equal(csvCell('plain'), '"plain"');
  assert.equal(csvCell(null), '""');
  assert.equal(csvCell(7), '"7"');
});

test('parseCursor takes created_at:id and nothing else (#509)', () => {
  assert.deepEqual(parseCursor('1725000000000:lic-abc123'), { at: 1725000000000, id: 'lic-abc123' });
  assert.equal(parseCursor(''), null);
  assert.equal(parseCursor('abc:lic-1'), null);
  assert.equal(parseCursor('1:lic 1'), null);
  assert.equal(parseCursor("1:lic-1' OR 1=1"), null);
});

test('machineOf wants a hex id and bounds the rest', () => {
  assert.equal(machineOf({}), null);
  assert.equal(machineOf({ machine: { id: 'not-hex' } }), null);
  assert.equal(machineOf({ machine: 'abcdef12' }), null);
  const m = machineOf({ machine: { id: 'abcdef1234567890', hostname: 'h'.repeat(200), user: 'u', version: '1.2.3' } });
  assert.equal(m.id, 'abcdef1234567890');
  assert.equal(m.hostname.length, 80);
  assert.equal(m.platform, '');
});

test('readBody: nothing is {}, an object is itself, anything else is null (#507)', async () => {
  const body = text => readBody(new Request('https://x', { method: 'POST', body: text }));
  assert.deepEqual(await body(''), {});
  assert.deepEqual(await body('  \n'), {});
  assert.deepEqual(await body('{"a":1}'), { a: 1 });
  assert.equal(await body('not json'), null);
  assert.equal(await body('[1,2]'), null);
  assert.equal(await body('"str"'), null);
  assert.equal(await body('null'), null);
});

test('timingSafeEqual compares whole strings', () => {
  assert.ok(timingSafeEqual('abc', 'abc'));
  assert.ok(!timingSafeEqual('abc', 'abd'));
});

// ---------------------------------------------------------------- secrets (#504)
test('login answers 500 without ADMIN_PASSWORD and without SESSION_SECRET', async () => {
  const DB = fakeDB();
  const attempt = env => admin.fetch(req('/admin/login', { method: 'POST', body: JSON.stringify({ password: 'pw' }) }), { DB, ...env });
  assert.equal((await attempt({ SESSION_SECRET: 's' })).status, 500);
  assert.equal((await attempt({ ADMIN_PASSWORD: 'pw' })).status, 500);
  assert.equal((await attempt({ ADMIN_PASSWORD: 'other', SESSION_SECRET: 's' })).status, 401);
  const ok = await attempt({ ADMIN_PASSWORD: 'pw', SESSION_SECRET: 's' });
  assert.equal(ok.status, 200);
  assert.match(ok.headers.get('set-cookie'), /sma_session=\d+\.[\w-]+\.[\w-]+; Path=\/; HttpOnly; Secure/);
  assert.ok(DB.ran("INSERT INTO events"), 'the login is logged');
});

test('a cookie signed with the placeholder key is not a session', async () => {
  const forged = await makeSession({ SESSION_SECRET: 'unset' });
  const r = await admin.fetch(req('/admin/api/stats', { headers: { cookie: `sma_session=${forged}` } }), { DB: fakeDB(), SESSION_SECRET: 'real' });
  assert.equal(r.status, 401);
  const nothing = await admin.fetch(req('/admin/api/stats', { headers: { cookie: `sma_session=${forged}` } }), { DB: fakeDB() });
  assert.equal(nothing.status, 500, 'no SESSION_SECRET at all is a configuration error, not a sign-in');
});

// ---------------------------------------------------------------- the admin API
async function signedIn(extra = {}) {
  const env = { DB: fakeDB({ 'FROM licences': [LICENCE], 'FROM settings': [] }), SESSION_SECRET: 'real', ADMIN_PASSWORD: 'pw', ...extra };
  const cookie = `sma_session=${await makeSession(env)}`;
  return { env, cookie };
}

test('a malformed body is 400, not {} (#507)', async () => {
  const { env, cookie } = await signedIn();
  const bad = await admin.fetch(req('/admin/api/licences/lic-1/renew', { method: 'POST', body: 'garbage', headers: { cookie } }), env);
  assert.equal(bad.status, 400);
  assert.ok(!env.DB.ran('UPDATE licences'), 'nothing was written');
  const list = await admin.fetch(req('/admin/api/licences/lic-1/renew', { method: 'POST', body: '[1]', headers: { cookie } }), env);
  assert.equal(list.status, 400);
});

test('renew needs expires and lifts a revocation only when asked (#507)', async () => {
  const key = await signingKey();
  const { env, cookie } = await signedIn({ SIGNING_KEY_PKCS8_B64: key });
  const missing = await admin.fetch(req('/admin/api/licences/lic-1/renew', { method: 'POST', body: '{"seats": 2}', headers: { cookie } }), env);
  assert.equal(missing.status, 400);
  assert.match((await missing.json()).detail, /expires/);

  const revokedEnv = { ...env, DB: fakeDB({ 'FROM licences': [{ ...LICENCE, revoked: 1 }], 'FROM settings': [] }) };
  const plain = await admin.fetch(req('/admin/api/licences/lic-1/renew', { method: 'POST', body: '{"expires": "2027-01-01"}', headers: { cookie } }), revokedEnv);
  assert.equal(plain.status, 200);
  const update = revokedEnv.DB.log.find(e => e.sql.startsWith('UPDATE licences SET expires'));
  assert.ok(update && !update.sql.includes('revoked = 0'), 'renewing did not restore');

  const restoring = { ...env, DB: fakeDB({ 'FROM licences': [{ ...LICENCE, revoked: 1 }], 'FROM settings': [] }) };
  await admin.fetch(req('/admin/api/licences/lic-1/renew', { method: 'POST', body: '{"expires": "", "restore": true}', headers: { cookie } }), restoring);
  const update2 = restoring.DB.log.find(e => e.sql.startsWith('UPDATE licences SET expires'));
  assert.ok(update2 && update2.sql.includes('revoked = 0'), 'restore: true lifts it');
  assert.equal(update2.args[0], '', 'a blank expiry is perpetual, on purpose');
});

test('a PUT changes only the fields it was given (#507)', async () => {
  const { env, cookie } = await signedIn();
  await admin.fetch(req('/admin/api/licences/lic-1', { method: 'PUT', body: '{"email": "new@example.com"}', headers: { cookie } }), env);
  const update = env.DB.log.find(e => e.sql.startsWith('UPDATE licences SET notes'));
  assert.deepEqual(update.args, ['n', 'new@example.com', 'lic-1']);
  const people = { ...env, DB: fakeDB({ 'FROM users': [{ id: 'usr-1', name: 'Ann', email: 'ann@example.com', org: 'Org', notes: 'kept' }] }) };
  await admin.fetch(req('/admin/api/users/usr-1', { method: 'PUT', body: '{"name": "Anne"}', headers: { cookie } }), people);
  const u = people.DB.log.find(e => e.sql.startsWith('UPDATE users'));
  assert.deepEqual(u.args, ['Anne', 'ann@example.com', 'Org', 'kept', 'usr-1']);
});

test('the mail test records nothing (#514)', async () => {
  const { env, cookie } = await signedIn({ RESEND_API_KEY: 'k' });
  const realFetch = globalThis.fetch;
  let sent = null;
  globalThis.fetch = async (url, init) => { sent = { url, body: JSON.parse(init.body) }; return new Response('{"id":"m"}', { status: 200 }); };
  try {
    const r = await admin.fetch(req('/admin/api/mail/test', { method: 'POST', body: '{"to": "me@example.com"}', headers: { cookie } }), env);
    assert.equal(r.status, 200);
    assert.equal(sent.url, 'https://api.resend.com/emails');
    assert.deepEqual(sent.body.to, ['me@example.com']);
    assert.ok(!env.DB.ran('UPDATE licences'), 'no licence row was touched');
    assert.ok(!env.DB.ran('INSERT INTO events'), 'no event for a licence called test');
  } finally { globalThis.fetch = realFetch; }
});

test('the portal page carries the one csvCell (#513)', async () => {
  const { env, cookie } = await signedIn();
  const page = await (await admin.fetch(req('/', { headers: { cookie } }), env)).text();
  assert.ok(page.includes('function csvCell(value)'), 'embedded');
  assert.ok(page.includes('columns.map(c => csvCell(r[c]))'), 'used by csv()');
});

// ---------------------------------------------------------------- the application endpoints
test('the app endpoints use APP_LIMITER when it exists, RATE_LIMITER otherwise (#508)', async () => {
  const DB = fakeDB();
  const both = await admin.fetch(req('/licence/check?id=lic-x'), { DB, RATE_LIMITER: limiter(false), APP_LIMITER: limiter(true) });
  assert.equal(both.status, 404, 'not limited: the small bucket being full does not matter');
  const only = await admin.fetch(req('/licence/check?id=lic-x'), { DB, RATE_LIMITER: limiter(false) });
  assert.equal(only.status, 429);
  const login = await admin.fetch(req('/admin/login', { method: 'POST', body: '{}' }), { DB, RATE_LIMITER: limiter(false), APP_LIMITER: limiter(true), ADMIN_PASSWORD: 'pw', SESSION_SECRET: 's' });
  assert.equal(login.status, 429, 'the login form keeps the small bucket');
});

test('refresh writes the installation and the counters in one batch, as an upsert (#512)', async () => {
  const DB = fakeDB({ 'FROM licences': [LICENCE], 'RETURNING seen_count': [{ seen_count: 1 }], 'COUNT(*) AS n': [{ n: 1 }] });
  const body = JSON.stringify({ id: 'lic-1', machine: { id: 'abcdef1234567890', hostname: 'pc', user: 'u', platform: 'Windows 11', version: '1.0.0' } });
  const r = await admin.fetch(req('/licence/refresh', { method: 'POST', body }), { DB });
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { id: 'lic-1', revoked: false, token: 'SM1.a.b', expires: '2026-12-31' });
  const upsert = DB.log.find(e => e.sql.includes('INSERT INTO activations'));
  assert.ok(upsert && upsert.sql.includes('ON CONFLICT(licence_id, machine_id) DO UPDATE'), 'one statement, no SELECT-then-INSERT');
  assert.ok(DB.ran('UPDATE licences SET last_refresh'));
  const activated = DB.log.find(e => e.sql.includes('INSERT INTO events') && e.args[2] === 'activated');
  assert.ok(activated, 'a first contact is logged as activated');
});

test('a refresh for an unknown key is 404 and nothing is written', async () => {
  const DB = fakeDB();
  const r = await admin.fetch(req('/licence/refresh', { method: 'POST', body: '{"id": "lic-nope"}' }), { DB });
  assert.equal(r.status, 404);
  assert.ok(!DB.ran('UPDATE') && !DB.ran('INSERT'));
});

// ---------------------------------------------------------------- the feedback relay (#515)
test('labelsFor answers only for its own keys', () => {
  assert.deepEqual(labelsFor('bug'), ['bug', 'user-reported']);
  assert.deepEqual(labelsFor('feature'), ['enhancement', 'user-reported']);
  for (const t of ['constructor', '__proto__', 'toString', 'hasOwnProperty', '', null, undefined, 42, ['bug']]) assert.equal(labelsFor(t), null, String(t));
});

test('the relay refuses a prototype-named type before touching GitHub', async () => {
  const realFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = async () => { called = true; return new Response('{}', { status: 201 }); };
  try {
    const env = { APP_KEY: 'k', GITHUB_TOKEN: 't', GITHUB_REPO: 'o/r' };
    const send = type => relay.fetch(new Request('https://relay.example/', { method: 'POST', headers: { 'X-ShellMate-Key': 'k' },
                                                  body: JSON.stringify({ type, title: 'T' }) }), env);
    assert.equal((await send('constructor')).status, 400);
    assert.equal((await send('__proto__')).status, 400);
    assert.ok(!called, 'GitHub was not asked');
    assert.equal((await send('bug')).status, 200);
    assert.ok(called);
  } finally { globalThis.fetch = realFetch; }
});
