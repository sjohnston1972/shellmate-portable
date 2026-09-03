/**
 * worker.js — The ShellMate licence service and its admin portal (#447).
 *
 * One Cloudflare Worker, three faces:
 *
 *   /licence/refresh, /licence/check      what the application calls
 *   /request                               a public page that issues a key
 *                                          on its own, when switched on
 *   /  and /admin/*                        the portal: issue, renew, revoke,
 *                                          email keys, people, reports
 *
 * Keys are Ed25519-signed tokens (see backend/licence.py). The private key
 * is a Worker secret; the public half ships inside ShellMate. Records live
 * in D1. The portal is a single page served here, styled after
 * workspace.foundry-ns.com, behind a password and an HMAC session cookie,
 * with hash routes so the browser's Back button means something.
 *
 * Email goes through Resend (https://resend.com) when RESEND_API_KEY is set
 * and the sender domain is verified there; without it the portal says so
 * and keys are copied by hand instead.
 *
 * Everything from the network is treated as hostile: inputs are bounded and
 * typed, the admin API needs the cookie, logins, refreshes and requests are
 * rate-limited per IP (the application endpoints on their own, wider,
 * bucket), and the application endpoints never return anything but the one
 * key they were asked about.
 *
 * Secrets: SIGNING_KEY_PKCS8_B64, ADMIN_PASSWORD, SESSION_SECRET,
 * RESEND_API_KEY (optional). Vars: PUBLIC_KEY_B64, PORTAL_TITLE.
 * The first three have no defaults: signing, the password login and the
 * session cookie each throw when theirs is missing, so a half-configured
 * deployment answers 500 rather than accepting a cookie anyone could forge.
 */

const SESSION_COOKIE = 'sma_session';
const SESSION_HOURS = 12;
const EVENT_KEEP_DAYS = 90;   // how long login and refreshed events are kept
const MAX = { name: 120, email: 200, org: 120, notes: 2000, reason: 300, seats: 100000, text: 4000 };
const SETTING_KEYS = ['requests_enabled', 'request_days', 'request_kind', 'mail_from', 'mail_subject', 'mail_intro', 'portal_notice'];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';
    try {
      if (path === '/health') return json(200, { ok: true, service: 'shellmate-admin' });
      if (path.startsWith('/licence/')) return application(request, env, path);
      if (path === '/request') return publicRequest(request, env);
      if (path === '/admin/login' && request.method === 'POST') return login(request, env);
      if (path === '/admin/logout') return logout();
      if (path.startsWith('/admin/api/')) {
        if (!(await authed(request, env))) return json(401, { detail: 'Sign in first.' });
        return adminApi(request, env, path);
      }
      if (path === '/' || path === '/admin') {
        const who = await accessIdentity(request, env);
        return html((who || await authed(request, env)) ? portalPage(env, who) : loginPage(env));
      }
      return json(404, { detail: 'Not found.' });
    } catch (err) {
      console.error(err && err.stack || err);
      return json(500, { detail: 'The service hit an error. It has been logged.' });
    }
  },

  // On the cron in wrangler.toml. Every login and every refresh appends an
  // event and nothing else ever removed one (#511). The kinds that are
  // noise after a season are pruned; issued, renewed, revoked, activated
  // and the rest are the licence's history and stay.
  async scheduled(event, env) {
    const r = await env.DB.prepare("DELETE FROM events WHERE kind IN ('login', 'refreshed') AND at < ?1")
      .bind(Date.now() - EVENT_KEEP_DAYS * 86400000).run();
    console.log(`events: pruned ${r.meta.changes} login/refreshed rows older than ${EVENT_KEEP_DAYS} days`);
  },
};

// ------------------------------------------------------------------ helpers
function json(status, body) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });
}
function html(body, status = 200, extra = {}) {
  return new Response(body, { status, headers: {
    'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store',
    'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer', ...extra } });
}
const enc = new TextEncoder();
function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function fromB64(text) {
  const bin = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - text.length % 4) % 4));
  return Uint8Array.from(bin, c => c.charCodeAt(0));
}
function clean(value, max) { return String(value == null ? '' : value).trim().slice(0, max); }
function isEmail(text) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text); }
function isoDate(value) {
  const text = clean(value, 32);
  if (!text) return '';
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text) || Number.isNaN(Date.parse(text))) throw new Error('Dates are YYYY-MM-DD.');
  return text;
}
function today() { return new Date().toISOString().slice(0, 10); }
function addDays(n) { const d = new Date(); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); }
function newId(prefix) {
  const raw = new Uint8Array(6); crypto.getRandomValues(raw);
  return prefix + '-' + [...raw].map(b => b.toString(16).padStart(2, '0')).join('');
}
// Two limiters, both per IP. RATE_LIMITER (20/min) guards the login form
// and the public request page, where twenty is generous. The application
// endpoints get APP_LIMITER, sized for the case org licences exist for: tens
// of seats behind one NAT all installing on the same morning, each an
// activate and a refresh. Sharing the small bucket with them answered 429
// to legitimate copies, which then went quiet for days (#508).
async function rateLimited(env, request, bucket) {
  const limiter = bucket === 'app' ? (env.APP_LIMITER || env.RATE_LIMITER) : env.RATE_LIMITER;
  if (!limiter) return false;
  const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
  const { success } = await limiter.limit({ key: `${bucket}:${ip}` });
  return !success;
}
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
// A request body as an object, {} for no body at all, or null when it is not
// JSON or not an object. A malformed body used to be read as {} and acted
// on: `renew` with no expiry signed a perpetual key and un-revoked it, and a
// PUT blanked every field it did not find (#507). Missing means missing.
async function readBody(request) {
  const text = await request.text().catch(() => '');
  if (!text.trim()) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch (_) { return null; }
}

// ------------------------------------------------------------------ signing
let signingKey = null;
async function signer(env) {
  if (!signingKey) {
    if (!env.SIGNING_KEY_PKCS8_B64) throw new Error('SIGNING_KEY_PKCS8_B64 is not set.');
    signingKey = await crypto.subtle.importKey('pkcs8', fromB64(env.SIGNING_KEY_PKCS8_B64),
                                               { name: 'Ed25519' }, false, ['sign']);
  }
  return signingKey;
}
async function signToken(env, payload) {
  const raw = enc.encode(JSON.stringify(payload));
  const sig = await crypto.subtle.sign({ name: 'Ed25519' }, await signer(env), raw);
  return `SM1.${b64url(raw)}.${b64url(sig)}`;
}
function payloadFor(row) {
  return {
    id: row.id, kind: row.kind, licensee: row.licensee, email: row.email,
    seats: Number(row.seats) || 1, issued: row.issued, expires: row.expires || '',
    grace_days: Number(row.grace_days) || 14, features: JSON.parse(row.features || '["updates"]'),
  };
}

// ------------------------------------------------------------------ sessions
// No default for the secret. With one, a deployment that forgot
// `wrangler secret put SESSION_SECRET` would accept any cookie signed with
// the default, which anyone can compute — full admin from a fresh
// environment, with nothing in the logs. Refusing is what signer() already
// does for the signing key; the session code does the same (#504).
async function hmac(env, text) {
  if (!env.SESSION_SECRET) throw new Error('SESSION_SECRET is not set.');
  const key = await crypto.subtle.importKey('raw', enc.encode(env.SESSION_SECRET),
                                            { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return b64url(await crypto.subtle.sign('HMAC', key, enc.encode(text)));
}
async function makeSession(env) {
  const nonce = new Uint8Array(16); crypto.getRandomValues(nonce);
  const body = `${Date.now() + SESSION_HOURS * 3600 * 1000}.${b64url(nonce)}`;
  return `${body}.${await hmac(env, body)}`;
}
// ---- Cloudflare Access: a valid identity from the team's Google login is a
// sign-in, so the password page is never seen behind Access. The JWT that
// Access attaches is verified against the team's published keys and the
// application's audience tag; a missing or bad one falls through to the
// password cookie, which remains as the fallback.
let accessCerts = { at: 0, keys: [] };
async function accessIdentity(request, env) {
  const jwt = request.headers.get('cf-access-jwt-assertion');
  if (!jwt || !env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) return null;
  const parts = jwt.split('.');
  if (parts.length !== 3) return null;
  try {
    const header = JSON.parse(new TextDecoder().decode(fromB64(parts[0])));
    const payload = JSON.parse(new TextDecoder().decode(fromB64(parts[1])));
    const now = Math.floor(Date.now() / 1000);
    if (payload.exp < now || payload.iss !== `https://${env.ACCESS_TEAM_DOMAIN}`) return null;
    const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
    if (!aud.includes(env.ACCESS_AUD)) return null;
    if (Date.now() - accessCerts.at > 3600 * 1000) {
      const resp = await fetch(`https://${env.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`);
      if (!resp.ok) return null;
      accessCerts = { at: Date.now(), keys: (await resp.json()).keys || [] };
    }
    const jwk = accessCerts.keys.find(k => k.kid === header.kid);
    if (!jwk) return null;
    const key = await crypto.subtle.importKey('jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
    const ok = await crypto.subtle.verify({ name: 'RSASSA-PKCS1-v1_5' }, key, fromB64(parts[2]), enc.encode(`${parts[0]}.${parts[1]}`));
    return ok ? { email: payload.email || '', sub: payload.sub || '' } : null;
  } catch (_) {
    return null;
  }
}

async function authed(request, env) {
  if (await accessIdentity(request, env)) return true;
  const cookie = request.headers.get('cookie') || '';
  const match = cookie.match(new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`));
  if (!match) return false;
  const parts = match[1].split('.');
  if (parts.length !== 3) return false;
  const [expires, nonce, mac] = parts;
  if (Number(expires) < Date.now()) return false;
  const expected = await hmac(env, `${expires}.${nonce}`);
  return expected.length === mac.length && timingSafeEqual(expected, mac);
}
function timingSafeEqual(a, b) {
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}
async function login(request, env) {
  if (await rateLimited(env, request, 'login')) return json(429, { detail: 'Too many attempts. Wait a minute.' });
  if (!env.ADMIN_PASSWORD) throw new Error('ADMIN_PASSWORD is not set.');
  let body = {};
  try { body = await request.json(); } catch (_) { return json(400, { detail: 'JSON expected.' }); }
  const given = String(body.password || '');
  const wanted = String(env.ADMIN_PASSWORD);
  if (given.length !== wanted.length || !timingSafeEqual(given, wanted)) {
    await new Promise(r => setTimeout(r, 400));
    return json(401, { detail: 'Wrong password.' });
  }
  const session = await makeSession(env);
  await logEvent(env, null, 'login', request.headers.get('CF-Connecting-IP') || '');
  return new Response(JSON.stringify({ ok: true }), { status: 200, headers: {
    'content-type': 'application/json',
    'set-cookie': `${SESSION_COOKIE}=${session}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_HOURS * 3600}`,
  } });
}
function logout() {
  return new Response(null, { status: 302, headers: {
    location: '/', 'set-cookie': `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0` } });
}

// ------------------------------------------------------------------ data
async function logEvent(env, licenceId, kind, detail) {
  await env.DB.prepare('INSERT INTO events (licence_id, at, kind, detail) VALUES (?1, ?2, ?3, ?4)')
    .bind(licenceId, Date.now(), kind, clean(detail, 500)).run();
}
// The licence row plus what the installations say about it. The
// installation figures come from one grouped pass over activations joined
// on, not three correlated subqueries per row: D1 bills rows read, and the
// old shape was up to four probes per licence on every list (#509). The
// join is scoped to one licence when only one is wanted, so a single lookup
// does not aggregate the whole table.
function installsJoin(scoped = false) {
  return `LEFT JOIN (SELECT licence_id, SUM(removed_at IS NULL) AS installs, MIN(first_seen) AS first_activated,
                            MAX(CASE WHEN removed_at IS NULL THEN last_seen END) AS last_seen
                     FROM activations${scoped ? ' WHERE licence_id = ?1' : ''} GROUP BY licence_id) i ON i.licence_id = licences.id`;
}
const LICENCE_COLS = 'licences.*, COALESCE(i.installs, 0) AS installs, i.first_activated, i.last_seen';
async function getLicence(env, id) {
  return env.DB.prepare(`SELECT ${LICENCE_COLS} FROM licences ${installsJoin(true)} WHERE licences.id = ?1`).bind(id).first();
}
// Paging on the licence list: newest first, the cursor naming the last row
// seen as created_at:id so a page boundary between two rows created in the
// same millisecond is still exact. Anything unparseable is no cursor.
function parseCursor(text) {
  const m = /^(\d{1,16}):([\w-]{1,64})$/.exec(clean(text, 100));
  return m ? { at: Number(m[1]), id: m[2] } : null;
}
function publicRow(row) {
  if (!row) return null;
  return { ...row, revoked: !!row.revoked, features: JSON.parse(row.features || '["updates"]') };
}
async function getSettings(env) {
  const rows = await env.DB.prepare('SELECT key, value FROM settings').all();
  const out = {};
  for (const r of rows.results) out[r.key] = r.value;
  return out;
}
async function setSetting(env, key, value) {
  await env.DB.prepare('INSERT INTO settings (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .bind(key, value).run();
}

/** Issue and record a licence. Shared by the portal and the request page. */
async function issue(env, spec) {
  const kind = spec.kind === 'org' ? 'org' : 'person';
  const licensee = clean(spec.licensee, MAX.name);
  if (!licensee) throw new Error('A licensee name is needed.');
  const seats = Math.min(MAX.seats, Math.max(1, parseInt(spec.seats, 10) || 1));
  const expires = isoDate(spec.expires);
  const issued = isoDate(spec.issued) || today();
  const grace = Math.min(365, Math.max(0, parseInt(spec.grace_days, 10) || 14));
  const features = Array.isArray(spec.features) && spec.features.length ? spec.features.map(f => clean(f, 40)) : ['updates'];
  const email = clean(spec.email, MAX.email);
  let userId = clean(spec.user_id, 64) || null;
  if (!userId && email) userId = await personFor(env, email, licensee, kind === 'org' ? licensee : clean(spec.org, MAX.org), spec.create_user !== false);
  const id = newId(kind === 'org' ? 'org' : 'lic');
  const row = { id, kind, licensee, email, seats, issued, expires, grace_days: grace, features: JSON.stringify(features) };
  const token = await signToken(env, payloadFor(row));
  await env.DB.prepare(`INSERT INTO licences (id, user_id, kind, licensee, email, seats, issued, expires, grace_days, features, token, notes, created_at, source)
                        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)`)
    .bind(id, userId, kind, licensee, email, seats, issued, expires, grace, row.features, token, clean(spec.notes, MAX.notes), Date.now(), spec.source || 'admin').run();
  await logEvent(env, id, 'issued', `${kind} · ${seats} seat(s) · expires ${expires || 'never'} · ${spec.source || 'admin'}`);
  return getLicence(env, id);
}

// The person for an address, created if there is none. One address is one
// person: users(lower(email)) is unique (schema-v4.sql), so when two
// requests for the same address arrive together the second INSERT is
// ignored rather than failing, and both end up on the row that won.
async function personFor(env, email, name, org, create) {
  const found = await env.DB.prepare('SELECT id FROM users WHERE lower(email) = lower(?1)').bind(email).first();
  if (found) return found.id;
  if (!create) return null;
  const id = newId('usr');
  const r = await env.DB.prepare('INSERT OR IGNORE INTO users (id, name, email, org, notes, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)')
    .bind(id, name, email, org, '', Date.now()).run();
  if (r.meta.changes) return id;
  const other = await env.DB.prepare('SELECT id FROM users WHERE lower(email) = lower(?1)').bind(email).first();
  return other ? other.id : null;
}
function isUniqueViolation(err) { return /UNIQUE constraint/i.test(err && err.message || ''); }

// ------------------------------------------------------------------ email
function mailConfigured(env) { return !!env.RESEND_API_KEY; }

async function sendKey(env, row, settings, kind = 'issued') {
  if (!mailConfigured(env)) throw new Error('Email is not configured: set the RESEND_API_KEY secret and verify the sender domain at resend.com.');
  if (!row.email || !isEmail(row.email)) throw new Error('This licence has no email address to send to.');
  const from = settings.mail_from || 'ShellMate <licences@foundry-ns.com>';
  const subject = settings.mail_subject || 'Your ShellMate licence key';
  const intro = settings.mail_intro || 'Your ShellMate licence key is below.';
  const text = [
    `Hello ${row.licensee},`, '',
    intro, '',
    row.token, '',
    `Licence: ${row.kind === 'org' ? `organisation, ${row.seats} seat(s)` : 'personal'} · issued ${row.issued} · expires ${row.expires || 'never'}.`,
    kind === 'renewed' ? 'This is a renewal. A copy of ShellMate that already has your previous key picks the new one up on its next refresh; nothing needs re-entering.' : '',
    '', 'ShellMate works without a licence; the key lets it update itself from inside the application.',
    '', 'Foundry Networks and Services — support@foundry-ns.com',
  ].filter(l => l !== null).join('\n');
  const htmlBody = `<div style="font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#0f172a;max-width:640px"><p>Hello ${esc(row.licensee)},</p><p>${esc(intro)}</p><pre style="font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#f6f7f9;border:1px solid #e6e8ec;border-radius:8px;padding:12px;white-space:pre-wrap;word-break:break-all">${esc(row.token)}</pre><p style="color:#64748b;font-size:13px">Licence: ${esc(row.kind === 'org' ? `organisation, ${row.seats} seat(s)` : 'personal')} · issued ${esc(row.issued)} · expires ${esc(row.expires || 'never')}.${kind === 'renewed' ? '<br>This is a renewal: a copy of ShellMate that has your previous key picks this one up on its next refresh.' : ''}</p><p style="color:#64748b;font-size:13px">ShellMate works without a licence; the key lets it update itself from inside the application.</p><p style="color:#94a3b8;font-size:12px">Foundry Networks and Services · support@foundry-ns.com</p></div>`;
  const resp = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, 'content-type': 'application/json' },
    body: JSON.stringify({ from, to: [row.email], subject, text, html: htmlBody,
      attachments: [{ filename: `shellmate-${row.id}.key`, content: btoa(row.token + '\n') }] }),
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '');
    throw new Error(`The mail service answered ${resp.status}: ${detail.slice(0, 200)}`);
  }
  await env.DB.prepare('UPDATE licences SET last_sent = ?1, sent_count = sent_count + 1 WHERE id = ?2').bind(Date.now(), row.id).run();
  await logEvent(env, row.id, 'emailed', `${kind} key to ${row.email}`);
}

// ------------------------------------------------------------------ installations
// Each copy of ShellMate that installs a key reports the machine it landed on
// and repeats itself at every refresh. One row per (licence, machine).
function machineOf(body) {
  const m = body && typeof body.machine === 'object' && body.machine ? body.machine : null;
  const id = m ? clean(m.id, 32) : '';
  if (!/^[0-9a-f]{8,32}$/.test(id)) return null;
  return { id, hostname: clean(m.hostname, 80), user: clean(m.user, 80), platform: clean(m.platform, 80), version: clean(m.version, 24) };
}
function describeMachine(m) {
  return `${m.hostname || m.id}${m.user ? ' (' + m.user + ')' : ''}${m.version ? ' · ShellMate ' + m.version : ''}`;
}
async function recordInstallation(env, row, machine, ip) {
  if (!machine) return;
  const now = Date.now();
  const existing = await env.DB.prepare('SELECT * FROM activations WHERE licence_id = ?1 AND machine_id = ?2').bind(row.id, machine.id).first();
  if (!existing) {
    await env.DB.prepare('INSERT INTO activations (licence_id, machine_id, hostname, user, platform, version, first_seen, last_seen, seen_count, last_ip) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?7, 1, ?8)')
      .bind(row.id, machine.id, machine.hostname, machine.user, machine.platform, machine.version, now, ip).run();
    await logEvent(env, row.id, 'activated', describeMachine(machine));
  } else {
    await env.DB.prepare('UPDATE activations SET hostname = ?1, user = ?2, platform = ?3, version = ?4, last_seen = ?5, seen_count = seen_count + 1, last_ip = ?6, removed_at = NULL WHERE licence_id = ?7 AND machine_id = ?8')
      .bind(machine.hostname, machine.user, machine.platform, machine.version, now, ip, row.id, machine.id).run();
    if (existing.removed_at) await logEvent(env, row.id, 'activated', describeMachine(machine) + ' — back after removal');
    else if (existing.version !== machine.version && machine.version) await logEvent(env, row.id, 'updated', `${machine.hostname || machine.id} now on ShellMate ${machine.version}`);
  }
  const n = (await env.DB.prepare('SELECT COUNT(*) AS n FROM activations WHERE licence_id = ?1 AND removed_at IS NULL').bind(row.id).first()).n;
  if (!existing && n > (Number(row.seats) || 1)) await logEvent(env, row.id, 'over-seats', `${n} installations on ${row.seats} seat(s)`);
  return n;
}

// ------------------------------------------------------------------ the application's endpoints
async function application(request, env, path) {
  if (await rateLimited(env, request, 'app')) return json(429, { detail: 'Too many requests.' });
  if (path === '/licence/refresh' && request.method === 'POST') {
    let body = {};
    try { body = await request.json(); } catch (_) { return json(400, { detail: 'JSON expected.' }); }
    const id = clean(body.id, 64);
    if (!id) return json(400, { detail: 'id is needed.' });
    const row = await getLicence(env, id);
    if (!row) return json(404, { detail: 'No such licence.' });
    const ip = request.headers.get('CF-Connecting-IP') || '';
    await env.DB.prepare('UPDATE licences SET last_refresh = ?1, refresh_count = refresh_count + 1, last_ip = ?2 WHERE id = ?3')
      .bind(Date.now(), ip, id).run();
    await recordInstallation(env, row, machineOf(body), ip);
    if (row.revoked) return json(200, { id, revoked: true, reason: row.revoked_reason || '' });
    return json(200, { id, revoked: false, token: row.token, expires: row.expires || '' });
  }
  if ((path === '/licence/activate' || path === '/licence/deactivate') && request.method === 'POST') {
    let body = {};
    try { body = await request.json(); } catch (_) { return json(400, { detail: 'JSON expected.' }); }
    const id = clean(body.id, 64);
    const machine = machineOf(body);
    if (!id || !machine) return json(400, { detail: 'id and machine are needed.' });
    const row = await getLicence(env, id);
    if (!row) return json(404, { detail: 'No such licence.' });
    const ip = request.headers.get('CF-Connecting-IP') || '';
    if (path === '/licence/deactivate') {
      const r = await env.DB.prepare('UPDATE activations SET removed_at = ?1, last_ip = ?2 WHERE licence_id = ?3 AND machine_id = ?4 AND removed_at IS NULL').bind(Date.now(), ip, id, machine.id).run();
      if (r.meta.changes) await logEvent(env, id, 'deactivated', describeMachine(machine));
      return json(200, { id, removed: !!r.meta.changes });
    }
    const n = await recordInstallation(env, row, machine, ip);
    return json(200, { id, revoked: !!row.revoked, expires: row.expires || '', installations: n, seats: Number(row.seats) || 1 });
  }
  if (path === '/licence/check' && request.method === 'GET') {
    const id = clean(new URL(request.url).searchParams.get('id'), 64);
    const row = id ? await getLicence(env, id) : null;
    if (!row) return json(404, { detail: 'No such licence.' });
    return json(200, { id, kind: row.kind, expires: row.expires || '', revoked: !!row.revoked });
  }
  return json(404, { detail: 'Not found.' });
}

// ------------------------------------------------------------------ the public request page
async function publicRequest(request, env) {
  const settings = await getSettings(env);
  const enabled = settings.requests_enabled === '1';
  if (request.method === 'GET') return html(requestPage(env, settings, enabled));
  if (request.method !== 'POST') return json(405, { detail: 'GET or POST.' });
  if (!enabled) return json(403, { detail: 'Licence requests are not open at the moment.' });
  if (await rateLimited(env, request, 'request')) return json(429, { detail: 'Too many requests. Try again in a minute.' });
  let body = {};
  try { body = await request.json(); } catch (_) { return json(400, { detail: 'JSON expected.' }); }
  const name = clean(body.name, MAX.name);
  const email = clean(body.email, MAX.email);
  if (!name || !isEmail(email)) return json(400, { detail: 'A name and a valid email address are needed.' });
  if (!mailConfigured(env)) return json(503, { detail: 'Keys are sent by email and email is not configured yet. Contact support@foundry-ns.com.' });
  // One live key per email: re-send it rather than issuing another.
  const existing = await env.DB.prepare("SELECT * FROM licences WHERE lower(email) = lower(?1) AND revoked = 0 AND (expires = '' OR expires >= ?2) ORDER BY created_at DESC LIMIT 1")
    .bind(email, today()).first();
  try {
    if (existing) {
      await sendKey(env, existing, settings, 'issued');
      await logEvent(env, existing.id, 'requested', `re-sent to ${email}`);
      return json(200, { ok: true, detail: 'A key for that address already exists; it has been sent again.' });
    }
    const days = Math.min(3650, Math.max(1, parseInt(settings.request_days, 10) || 30));
    const row = await issue(env, { kind: settings.request_kind === 'org' ? 'org' : 'person', licensee: name, email,
                                   org: clean(body.org, MAX.org), expires: addDays(days), notes: 'self-service request', source: 'request' });
    await sendKey(env, row, settings, 'issued');
    return json(201, { ok: true, detail: `Your key has been sent to ${email}. It is valid for ${days} days.` });
  } catch (err) {
    return json(502, { detail: err.message });
  }
}

// ------------------------------------------------------------------ the admin API
async function adminApi(request, env, path) {
  const url = new URL(request.url);
  const method = request.method;
  let body = {};
  if (method === 'POST' || method === 'PUT') {
    body = await readBody(request);
    if (body === null) return json(400, { detail: 'The body must be a JSON object.' });
  }
  const settings = await getSettings(env);

  if (path === '/admin/api/stats') {
    const t = today();
    const [total, active, expiring, expired, revoked, users, month] = await Promise.all([
      env.DB.prepare('SELECT COUNT(*) AS n FROM licences').first(),
      env.DB.prepare("SELECT COUNT(*) AS n FROM licences WHERE revoked = 0 AND (expires = '' OR expires >= ?1)").bind(t).first(),
      env.DB.prepare("SELECT COUNT(*) AS n FROM licences WHERE revoked = 0 AND expires != '' AND expires >= ?1 AND expires <= ?2").bind(t, addDays(30)).first(),
      env.DB.prepare("SELECT COUNT(*) AS n FROM licences WHERE revoked = 0 AND expires != '' AND expires < ?1").bind(t).first(),
      env.DB.prepare('SELECT COUNT(*) AS n FROM licences WHERE revoked = 1').first(),
      env.DB.prepare('SELECT COUNT(*) AS n FROM users').first(),
      env.DB.prepare("SELECT COUNT(*) AS n FROM licences WHERE created_at >= ?1").bind(Date.now() - 30 * 86400000).first(),
    ]);
    const inForce = "revoked = 0 AND (expires = '' OR expires >= ?1)";
    const [activated, installs, seen7] = await Promise.all([
      env.DB.prepare(`SELECT COUNT(*) AS n FROM licences WHERE ${inForce} AND EXISTS (SELECT 1 FROM activations a WHERE a.licence_id = licences.id AND a.removed_at IS NULL)`).bind(t).first(),
      env.DB.prepare('SELECT COUNT(*) AS n FROM activations WHERE removed_at IS NULL').first(),
      env.DB.prepare('SELECT COUNT(*) AS n FROM activations WHERE removed_at IS NULL AND last_seen >= ?1').bind(Date.now() - 7 * 86400000).first(),
    ]);
    // By id, not by at: ids are AUTOINCREMENT and so in the same order as
    // at, and the rowid is the table's own index, where ordering by at
    // meant sorting the whole log on every visit (#511).
    const recent = await env.DB.prepare('SELECT e.*, l.licensee FROM events e LEFT JOIN licences l ON l.id = e.licence_id ORDER BY e.id DESC LIMIT 15').all();
    return json(200, { licences: total.n, active: active.n, expiring: expiring.n, expired: expired.n, revoked: revoked.n,
                       users: users.n, issued_30d: month.n, events: recent.results,
                       activated: activated.n, unactivated: active.n - activated.n, installations: installs.n, seen_7d: seen7.n,
                       mail: mailConfigured(env), requests: settings.requests_enabled === '1', notice: settings.portal_notice || '' });
  }

  if (path === '/admin/api/reports') {
    const t = today();
    const [byMonth, byKind, bySource, expiring, seats] = await Promise.all([
      env.DB.prepare("SELECT substr(issued,1,7) AS month, COUNT(*) AS n FROM licences GROUP BY month ORDER BY month DESC LIMIT 24").all(),
      env.DB.prepare('SELECT kind, COUNT(*) AS n, SUM(seats) AS seats FROM licences WHERE revoked = 0 GROUP BY kind').all(),
      env.DB.prepare('SELECT source, COUNT(*) AS n FROM licences GROUP BY source').all(),
      env.DB.prepare("SELECT * FROM licences WHERE revoked = 0 AND expires != '' AND expires >= ?1 AND expires <= ?2 ORDER BY expires").bind(t, addDays(90)).all(),
      env.DB.prepare("SELECT SUM(seats) AS n FROM licences WHERE revoked = 0 AND (expires = '' OR expires >= ?1)").bind(t).first(),
    ]);
    const [versions, unactivated, overSeats] = await Promise.all([
      env.DB.prepare("SELECT version, COUNT(*) AS n FROM activations WHERE removed_at IS NULL GROUP BY version ORDER BY version DESC").all(),
      env.DB.prepare(`SELECT ${LICENCE_COLS} FROM licences ${installsJoin()} WHERE revoked = 0 AND (expires = '' OR expires >= ?1) AND created_at <= ?2 AND COALESCE(i.installs, 0) = 0 ORDER BY created_at LIMIT 500`).bind(t, Date.now() - 7 * 86400000).all(),
      env.DB.prepare(`SELECT ${LICENCE_COLS} FROM licences ${installsJoin()} WHERE revoked = 0 AND seats < COALESCE(i.installs, 0) ORDER BY installs DESC LIMIT 500`).all(),
    ]);
    const buckets = { d30: 0, d60: 0, d90: 0 };
    for (const l of expiring.results) {
      if (l.expires <= addDays(30)) buckets.d30 += 1; else if (l.expires <= addDays(60)) buckets.d60 += 1; else buckets.d90 += 1;
    }
    return json(200, { by_month: byMonth.results.reverse(), by_kind: byKind.results, by_source: bySource.results,
                       expiring: expiring.results.map(publicRow), buckets, seats_in_force: seats.n || 0,
                       versions: versions.results, unactivated: unactivated.results.map(publicRow), over_seats: overSeats.results.map(publicRow) });
  }

  if (path === '/admin/api/settings' && method === 'GET') {
    return json(200, { settings, mail: mailConfigured(env), public_key: env.PUBLIC_KEY_B64 || '',
                       request_url: `${url.origin}/request` });
  }
  if (path === '/admin/api/settings' && method === 'PUT') {
    for (const key of SETTING_KEYS) {
      if (key in body) await setSetting(env, key, key === 'requests_enabled' ? (body[key] ? '1' : '0') : clean(body[key], MAX.text));
    }
    return json(200, { settings: await getSettings(env) });
  }
  if (path === '/admin/api/mail/test' && method === 'POST') {
    const to = clean(body.to, MAX.email);
    if (!isEmail(to)) return json(400, { detail: 'A valid address is needed.' });
    try {
      await sendKey(env, { id: 'test', licensee: 'Test', email: to, kind: 'person', seats: 1, issued: today(), expires: '', token: 'SM1.test.test' }, settings, 'issued')
        .catch(err => { if (!/UPDATE|no such/i.test(err.message)) throw err; });
      return json(200, { ok: true });
    } catch (err) { return json(502, { detail: err.message }); }
  }

  if (path === '/admin/api/licences' && method === 'GET') {
    const q = clean(url.searchParams.get('q'), 120).toLowerCase();
    const status = clean(url.searchParams.get('status'), 20);
    const kind = clean(url.searchParams.get('kind'), 10);
    const where = []; const args = [];
    if (q) { where.push('(lower(licensee) LIKE ? OR lower(email) LIKE ? OR lower(id) LIKE ? OR lower(notes) LIKE ?)'); args.push(`%${q}%`, `%${q}%`, `%${q}%`, `%${q}%`); }
    if (kind === 'person' || kind === 'org') { where.push('kind = ?'); args.push(kind); }
    const t = today();
    if (status === 'active') { where.push("revoked = 0 AND (expires = '' OR expires > ?)"); args.push(addDays(30)); }
    else if (status === 'expiring') { where.push("revoked = 0 AND expires != '' AND expires >= ? AND expires <= ?"); args.push(t, addDays(30)); }
    else if (status === 'expired') { where.push("revoked = 0 AND expires != '' AND expires < ?"); args.push(t); }
    else if (status === 'revoked') { where.push('revoked = 1'); }
    else if (status === 'activated') { where.push("revoked = 0 AND (expires = '' OR expires >= ?) AND COALESCE(i.installs, 0) > 0"); args.push(t); }
    else if (status === 'unactivated') { where.push("revoked = 0 AND (expires = '' OR expires >= ?) AND COALESCE(i.installs, 0) = 0"); args.push(t); }
    else if (status === 'overseats') { where.push('revoked = 0 AND seats < COALESCE(i.installs, 0)'); }
    const cursor = parseCursor(url.searchParams.get('cursor'));
    if (cursor) { where.push('(created_at < ? OR (created_at = ? AND licences.id < ?))'); args.push(cursor.at, cursor.at, cursor.id); }
    const limit = Math.min(500, Math.max(1, parseInt(url.searchParams.get('limit'), 10) || 200));
    // One more than asked for says whether there is a next page without a
    // second COUNT query; it is not returned.
    const sql = `SELECT ${LICENCE_COLS} FROM licences ${installsJoin()}` + (where.length ? ' WHERE ' + where.join(' AND ') : '')
              + ` ORDER BY created_at DESC, licences.id DESC LIMIT ${limit + 1}`;
    const rows = (await env.DB.prepare(sql).bind(...args).all()).results;
    const page = rows.slice(0, limit);
    const last = page[page.length - 1];
    return json(200, { licences: page.map(publicRow), next_cursor: rows.length > limit ? `${last.created_at}:${last.id}` : '' });
  }

  if (path === '/admin/api/licences' && method === 'POST') {
    try {
      const row = await issue(env, { ...body, source: 'admin' });
      let mailed = false, mail_error = '';
      if (body.send !== false && row.email && mailConfigured(env)) {
        try { await sendKey(env, row, settings, 'issued'); mailed = true; } catch (err) { mail_error = err.message; }
      }
      return json(201, { licence: publicRow(await getLicence(env, row.id)), mailed, mail_error });
    } catch (err) { return json(400, { detail: err.message }); }
  }

  const m = path.match(/^\/admin\/api\/licences\/([^/]+)(?:\/(revoke|restore|renew|send|activations)(?:\/([^/]+))?)?$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    const row = await getLicence(env, id);
    if (!row) return json(404, { detail: 'No such licence.' });
    const action = m[2];
    if (!action && method === 'GET') {
      const events = await env.DB.prepare('SELECT * FROM events WHERE licence_id = ?1 ORDER BY at DESC LIMIT 100').bind(id).all();
      const user = row.user_id ? await env.DB.prepare('SELECT * FROM users WHERE id = ?1').bind(row.user_id).first() : null;
      const activations = await env.DB.prepare('SELECT * FROM activations WHERE licence_id = ?1 ORDER BY (removed_at IS NOT NULL), last_seen DESC').bind(id).all();
      return json(200, { licence: publicRow(row), events: events.results, user, activations: activations.results });
    }
    if (action === 'activations' && method === 'DELETE' && m[3]) {
      // Forget an installation: frees the seat. The copy re-registers at its next refresh if it is still there.
      const machine = clean(decodeURIComponent(m[3]), 32);
      const a = await env.DB.prepare('SELECT * FROM activations WHERE licence_id = ?1 AND machine_id = ?2').bind(id, machine).first();
      if (!a) return json(404, { detail: 'No such installation.' });
      await env.DB.prepare('DELETE FROM activations WHERE licence_id = ?1 AND machine_id = ?2').bind(id, machine).run();
      await logEvent(env, id, 'forgotten', describeMachine({ id: machine, hostname: a.hostname, user: a.user, version: a.version }));
      return json(200, { deleted: true });
    }
    if (!action && method === 'DELETE') {
      await env.DB.prepare('DELETE FROM licences WHERE id = ?1').bind(id).run();
      await logEvent(env, id, 'deleted', row.licensee);
      return json(200, { deleted: true });
    }
    if (!action && method === 'PUT') {
      // Only the fields that were sent change; a body without `notes` is
      // not a request to empty them.
      await env.DB.prepare('UPDATE licences SET notes = ?1, email = ?2 WHERE id = ?3')
        .bind('notes' in body ? clean(body.notes, MAX.notes) : row.notes,
              'email' in body ? clean(body.email, MAX.email) : row.email, id).run();
      return json(200, { licence: publicRow(await getLicence(env, id)) });
    }
    if (action === 'revoke' && method === 'POST') {
      const reason = clean(body.reason, MAX.reason);
      await env.DB.prepare('UPDATE licences SET revoked = 1, revoked_reason = ?1 WHERE id = ?2').bind(reason, id).run();
      await logEvent(env, id, 'revoked', reason);
      return json(200, { licence: publicRow(await getLicence(env, id)) });
    }
    if (action === 'restore' && method === 'POST') {
      await env.DB.prepare("UPDATE licences SET revoked = 0, revoked_reason = '' WHERE id = ?1").bind(id).run();
      await logEvent(env, id, 'restored', '');
      return json(200, { licence: publicRow(await getLicence(env, id)) });
    }
    if (action === 'renew' && method === 'POST') {
      // The expiry must be stated, blank meaning perpetual on purpose; a
      // body that simply lacks it is refused rather than read as "never".
      // Revocation is lifted only when asked: renewing is not restoring.
      if (!('expires' in body)) return json(400, { detail: 'expires is needed: a date, or blank for a perpetual key.' });
      let expires;
      try { expires = isoDate(body.expires); } catch (err) { return json(400, { detail: err.message }); }
      const seats = body.seats ? Math.min(MAX.seats, Math.max(1, parseInt(body.seats, 10) || row.seats)) : row.seats;
      const restore = !!row.revoked && body.restore === true;
      const updated = { ...row, expires, seats, issued: today() };
      const token = await signToken(env, payloadFor(updated));
      await env.DB.prepare(`UPDATE licences SET expires = ?1, seats = ?2, issued = ?3, token = ?4${restore ? ", revoked = 0, revoked_reason = ''" : ''} WHERE id = ?5`)
        .bind(expires, seats, updated.issued, token, id).run();
      await logEvent(env, id, 'renewed', `expires ${expires || 'never'} · ${seats} seat(s)${restore ? ' · restored' : ''}`);
      let mailed = false, mail_error = '';
      const fresh = await getLicence(env, id);
      if (body.send !== false && fresh.email && mailConfigured(env)) {
        try { await sendKey(env, fresh, settings, 'renewed'); mailed = true; } catch (err) { mail_error = err.message; }
      }
      return json(200, { licence: publicRow(await getLicence(env, id)), mailed, mail_error });
    }
    if (action === 'send' && method === 'POST') {
      try { await sendKey(env, row, settings, body.kind === 'renewed' ? 'renewed' : 'issued'); }
      catch (err) { return json(502, { detail: err.message }); }
      return json(200, { licence: publicRow(await getLicence(env, id)) });
    }
  }

  if (path === '/admin/api/users' && method === 'GET') {
    const q = clean(url.searchParams.get('q'), 120).toLowerCase();
    const base = 'SELECT u.*, (SELECT COUNT(*) FROM licences l WHERE l.user_id = u.id) AS licences, (SELECT COUNT(*) FROM licences l WHERE l.user_id = u.id AND l.revoked = 0 AND (l.expires = \'\' OR l.expires >= ?1)) AS active FROM users u';
    const rows = q
      ? await env.DB.prepare(base + ' WHERE lower(name) LIKE ?2 OR lower(email) LIKE ?2 OR lower(org) LIKE ?2 ORDER BY created_at DESC LIMIT 1000').bind(today(), `%${q}%`).all()
      : await env.DB.prepare(base + ' ORDER BY created_at DESC LIMIT 1000').bind(today()).all();
    return json(200, { users: rows.results });
  }
  if (path === '/admin/api/users' && method === 'POST') {
    const name = clean(body.name, MAX.name);
    if (!name) return json(400, { detail: 'A name is needed.' });
    const id = newId('usr');
    try {
      await env.DB.prepare('INSERT INTO users (id, name, email, org, notes, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)')
        .bind(id, name, clean(body.email, MAX.email), clean(body.org, MAX.org), clean(body.notes, MAX.notes), Date.now()).run();
    } catch (err) {
      if (isUniqueViolation(err)) return json(409, { detail: 'A person with that email address already exists.' });
      throw err;
    }
    return json(201, { user: await env.DB.prepare('SELECT * FROM users WHERE id = ?1').bind(id).first() });
  }
  const u = path.match(/^\/admin\/api\/users\/([^/]+)$/);
  if (u) {
    const id = decodeURIComponent(u[1]);
    const row = await env.DB.prepare('SELECT * FROM users WHERE id = ?1').bind(id).first();
    if (!row) return json(404, { detail: 'No such person.' });
    if (method === 'GET') {
      const licences = await env.DB.prepare('SELECT * FROM licences WHERE user_id = ?1 ORDER BY created_at DESC').bind(id).all();
      return json(200, { user: row, licences: licences.results.map(publicRow) });
    }
    if (method === 'PUT') {
      const field = (key, max) => (key in body ? clean(body[key], max) : row[key]);
      try {
        await env.DB.prepare('UPDATE users SET name = ?1, email = ?2, org = ?3, notes = ?4 WHERE id = ?5')
          .bind(field('name', MAX.name) || row.name, field('email', MAX.email), field('org', MAX.org), field('notes', MAX.notes), id).run();
      } catch (err) {
        if (isUniqueViolation(err)) return json(409, { detail: 'Another person already has that email address.' });
        throw err;
      }
      return json(200, { user: await env.DB.prepare('SELECT * FROM users WHERE id = ?1').bind(id).first() });
    }
    if (method === 'DELETE') {
      await env.DB.prepare('DELETE FROM users WHERE id = ?1').bind(id).run();
      return json(200, { deleted: true });
    }
  }
  return json(404, { detail: 'Not found.' });
}

// ------------------------------------------------------------------ pages
const STYLE = `
:root { --accent:#00a3ff; --accent-soft:#e6f5ff; --ink:#0f172a; --ink-2:#334155; --ink-3:#64748b; --ink-4:#94a3b8;
  --bg:#f6f7f9; --card:#fcfcfd; --line:#e6e8ec; --line-2:#d7dbe0; --danger:#b03a32; --warn:#b07208; --ok:#2c7a42;
  --font-sans:"Inter",ui-sans-serif,system-ui,sans-serif; --font-display:"Space Grotesk",ui-sans-serif,system-ui,sans-serif; --font-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,monospace;
  --radius:12px; --radius-sm:8px; }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 var(--font-sans); }
a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
h1,h2,h3 { font-family:var(--font-display); margin:0; letter-spacing:-.01em } h1{font-size:22px} h2{font-size:17px} h3{font-size:14px}
.mono { font-family:var(--font-mono); font-size:12px }
.app { display:grid; grid-template-columns:232px 1fr; min-height:100vh }
.side { background:#fff; border-right:1px solid var(--line); padding:20px 14px; display:flex; flex-direction:column; gap:4px; position:sticky; top:0; height:100vh }
.brand { display:flex; align-items:center; gap:10px; padding:4px 8px 18px; font-family:var(--font-display); font-weight:700; font-size:16px }
.brand .dot { width:28px; height:28px; border-radius:8px; background:linear-gradient(135deg,var(--accent),#7cd4ff); display:grid; place-items:center; color:#fff; font-size:13px }
.nav { display:flex; align-items:center; gap:10px; padding:9px 10px; border-radius:var(--radius-sm); color:var(--ink-2); cursor:pointer; font-weight:500 }
.nav:hover { background:var(--bg) } .nav.active { background:var(--accent-soft); color:#0369a1 }
.nav svg { width:16px; height:16px; stroke:currentColor; fill:none; stroke-width:2 }
.side .foot { margin-top:auto; font-size:12px; color:var(--ink-4); padding:8px 10px }
.main { padding:28px 32px; max-width:1180px }
.top { display:flex; align-items:center; justify-content:space-between; margin-bottom:22px; gap:16px; flex-wrap:wrap }
.top .sub { color:var(--ink-3); margin-top:2px }
.crumbs { font-size:12px; color:var(--ink-4); margin-bottom:8px } .crumbs a { color:var(--ink-3) }
.btn { font:inherit; font-weight:600; padding:8px 14px; border-radius:var(--radius-sm); border:1px solid var(--line-2); background:#fff; color:var(--ink-2); cursor:pointer; display:inline-flex; align-items:center; gap:6px }
.btn:hover { background:var(--bg) } .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff } .btn.primary:hover { filter:brightness(.95) }
.btn.danger { color:var(--danger); border-color:#e8c4c1 } .btn.sm { padding:5px 10px; font-size:12px } .btn:disabled { opacity:.5; cursor:default }
.cards { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:22px }
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px }
.card.click { cursor:pointer } .card.click:hover { border-color:var(--accent) }
.card .k { color:var(--ink-3); font-size:12px; font-weight:500 } .card .v { font-family:var(--font-display); font-size:26px; font-weight:700; margin-top:4px }
.panel { background:#fff; border:1px solid var(--line); border-radius:var(--radius); overflow:hidden; margin-bottom:18px }
.panel .bar { display:flex; align-items:center; gap:10px; padding:12px 16px; border-bottom:1px solid var(--line); flex-wrap:wrap }
.panel .bar input[type=search], .panel .bar input[type=text] { flex:1; min-width:200px; max-width:360px }
.panel .bar select { width:auto }
.panel .body { padding:18px }
input, select, textarea { font:inherit; padding:8px 10px; border:1px solid var(--line-2); border-radius:var(--radius-sm); background:#fff; color:var(--ink); width:100% }
input[type=checkbox] { width:auto } input:focus, select:focus, textarea:focus { outline:2px solid var(--accent-soft); border-color:var(--accent) }
table { width:100%; border-collapse:collapse } th { text-align:left; font-size:12px; color:var(--ink-3); font-weight:600; padding:10px 16px; border-bottom:1px solid var(--line); background:var(--card); white-space:nowrap }
th.sort { cursor:pointer; user-select:none } th.sort:hover { color:var(--ink) } th .dir { font-size:10px; margin-left:4px }
td { padding:11px 16px; border-bottom:1px solid var(--line); vertical-align:top } tr:hover td { background:#fafbfc } tr.click { cursor:pointer }
.pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600 }
.pill.ok { background:#e7f5ec; color:var(--ok) } .pill.warn { background:#fdf3e1; color:var(--warn) } .pill.bad { background:#fbe9e7; color:var(--danger) } .pill.grey { background:var(--bg); color:var(--ink-3) } .pill.blue { background:var(--accent-soft); color:#0369a1 }
.form { display:grid; grid-template-columns:1fr 1fr; gap:14px 18px } .form .full { grid-column:1/-1 } label { display:block; font-size:12px; font-weight:600; color:var(--ink-2); margin-bottom:5px } .hint { font-size:12px; color:var(--ink-4); margin-top:4px }
.key { font-family:var(--font-mono); font-size:11px; word-break:break-all; background:var(--bg); border:1px solid var(--line); border-radius:var(--radius-sm); padding:10px; user-select:all }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap } .grow { flex:1 }
.kv { display:grid; grid-template-columns:140px 1fr; gap:6px 12px; font-size:13px } .kv .k { color:var(--ink-3) }
.events { list-style:none; padding:0; margin:0 } .events li { padding:8px 0; border-bottom:1px solid var(--line); font-size:13px } .events time { color:var(--ink-4); font-size:12px; margin-right:8px }
.toast { position:fixed; right:20px; bottom:20px; background:var(--ink); color:#fff; padding:10px 14px; border-radius:var(--radius-sm); font-size:13px; z-index:30; max-width:420px }
.login { min-height:100vh; display:grid; place-items:center } .login .card { width:360px; padding:28px }
.empty { padding:36px; text-align:center; color:var(--ink-4) }
.notice { background:#fff8e6; border:1px solid #f1dfae; color:#7a5a08; padding:10px 14px; border-radius:var(--radius-sm); margin-bottom:18px; font-size:13px }
.bars { display:flex; align-items:flex-end; gap:6px; height:120px; padding:8px 0 } .bars .b { flex:1; background:var(--accent-soft); border:1px solid #bfe4ff; border-radius:4px 4px 0 0; position:relative; min-width:14px }
.bars .b span { position:absolute; bottom:100%; left:0; right:0; text-align:center; font-size:10px; color:var(--ink-3) } .bars .b i { position:absolute; top:100%; left:0; right:0; text-align:center; font-size:10px; color:var(--ink-4); font-style:normal; margin-top:4px }
.two { display:grid; grid-template-columns:1fr 1fr; gap:18px }
.request { max-width:520px; margin:8vh auto; padding:0 16px }
@media (max-width:900px){ .app{grid-template-columns:1fr} .side{display:none} .cards{grid-template-columns:1fr 1fr} .form,.two{grid-template-columns:1fr} }
`;
const FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">';

function loginPage(env) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</title>${FONTS}<style>${STYLE}</style></head>
<body><div class="login"><div class="card"><div class="brand"><span class="dot">S</span> ${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</div>
<form id="f"><label>Password</label><input type="password" id="p" autofocus autocomplete="current-password"><div style="height:14px"></div><button class="btn primary" style="width:100%;justify-content:center">Sign in</button><p class="hint" id="err"></p></form></div></div>
<script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const r=await fetch('/admin/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:document.getElementById('p').value})});if(r.ok)location.reload();else document.getElementById('err').textContent=(await r.json()).detail||'Wrong password.';};</script></body></html>`;
}

function requestPage(env, settings, enabled) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ShellMate licence</title>${FONTS}<style>${STYLE}</style></head>
<body><div class="request"><div class="brand"><span class="dot">S</span> ShellMate licence</div>
<div class="card" style="padding:24px">${enabled ? `
<h2>Request a licence key</h2><p class="hint" style="margin:6px 0 16px">The key is sent to your email and is valid for ${esc(settings.request_days || '30')} days. ShellMate works without one; the key lets it update itself from inside the application.</p>
<form id="f" class="form"><div class="full"><label>Name</label><input id="name" required maxlength="120"></div><div class="full"><label>Email</label><input id="email" type="email" required maxlength="200"></div><div class="full"><label>Organisation (optional)</label><input id="org" maxlength="120"></div>
<div class="full"><button class="btn primary" id="go">Send me a key</button></div></form><p class="hint" id="msg"></p>
<script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const b=document.getElementById('go');b.disabled=true;const r=await fetch('/request',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name:document.getElementById('name').value,email:document.getElementById('email').value,org:document.getElementById('org').value})});const d=await r.json().catch(()=>({}));document.getElementById('msg').textContent=d.detail||(r.ok?'Sent.':'Something went wrong.');document.getElementById('msg').style.color=r.ok?'var(--ok)':'var(--danger)';b.disabled=false;};</script>`
: `<h2>Licence requests are closed</h2><p class="hint" style="margin-top:6px">Keys are issued by Foundry Networks and Services. Write to <a href="mailto:support@foundry-ns.com">support@foundry-ns.com</a>.</p>`}
</div></div></body></html>`;
}

function portalPage(env, who) {
  const nav = [
    ['overview', 'Overview', '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>'],
    ['licences', 'Licences', '<path d="M21 2l-2 2m-7.6 7.6a5 5 0 1 1-7 7 5 5 0 0 1 7-7zm0 0L19 3.5M15 5l2 2"/>'],
    ['people', 'People', '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>'],
    ['issue', 'Issue a licence', '<circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/>'],
    ['reports', 'Reports', '<path d="M3 3v18h18"/><path d="M7 15l4-4 4 4 5-6"/>'],
    ['settings', 'Settings', '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'],
  ].map(([v, t, p]) => `<a class="nav" data-view="${v}" href="#/${v}"><svg viewBox="0 0 24 24">${p}</svg>${t}</a>`).join('');
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</title>${FONTS}<style>${STYLE}</style></head>
<body><div class="app"><aside class="side"><div class="brand"><span class="dot">S</span> ${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</div>${nav}
<div class="foot">${who && who.email ? esc(who.email) + '<br>via Cloudflare Access<br>' : ''}<a href="/request" target="_blank">Public request page</a><br><a href="${who ? '/cdn-cgi/access/logout' : '/admin/logout'}">Sign out</a></div></aside>
<main class="main" id="main"></main></div><script>${PORTAL_JS}</script></body></html>`;
}

const PORTAL_JS = String.raw`
const $ = (s, r=document) => r.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async (path, opts={}) => { const r = await fetch(path, {headers:{'content-type':'application/json'}, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined}); const d = await r.json().catch(()=>({})); if (!r.ok) throw new Error(d.detail || ('HTTP '+r.status)); return d; };
const toast = m => { const t = document.createElement('div'); t.className='toast'; t.textContent=m; document.body.appendChild(t); setTimeout(()=>t.remove(), 3200); };
const when = ts => ts ? new Date(ts).toLocaleString() : '—';
const today = () => new Date().toISOString().slice(0,10);
const addDays = n => { const d=new Date(); d.setDate(d.getDate()+n); return d.toISOString().slice(0,10); };
const installed = l => !l.installs ? '<span class="pill grey">not yet</span>' : '<span class="pill '+(l.installs > l.seats ? 'warn' : 'ok')+'">'+l.installs+' of '+l.seats+'</span>'+(l.last_seen ? ' <span class="hint">seen '+esc(when(l.last_seen))+'</span>' : '');
const status = l => l.revoked ? ['bad','revoked'] : !l.expires ? ['ok','perpetual'] : (l.expires < today() ? ['bad','expired'] : (l.expires <= addDays(30) ? ['warn','expiring'] : ['ok','active']));
const main = $('#main');
function debounce(f, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; }
function crumbs(...parts) { return '<div class="crumbs">' + parts.map((p, i) => i < parts.length - 1 ? '<a href="#/'+p[1]+'">'+esc(p[0])+'</a> › ' : esc(p[0])).join('') + '</div>'; }
function back(hash, label) { return '<a class="btn sm" href="#/'+hash+'">← '+esc(label)+'</a>'; }
function csv(rows, columns, name) { const lines = [columns.join(',')].concat(rows.map(r => columns.map(c => '"'+String(r[c]==null?'':r[c]).replace(/"/g,'""')+'"').join(','))); const a = document.createElement('a'); a.href = 'data:text/csv;charset=utf-8,'+encodeURIComponent(lines.join('\n')); a.download = name; a.click(); }

// ---- router: #/view, #/view/id, #/view?q=
function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'overview';
  const [pathPart, query] = hash.split('?');
  const [view, id] = pathPart.split('/');
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  document.querySelectorAll('.nav').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  const pages = { overview, licences, licence: detail, people, person, issue, reports, settings };
  const fn = pages[view] || overview;
  Promise.resolve(fn(id ? decodeURIComponent(id) : params)).catch(e => { main.innerHTML = '<div class="empty">'+esc(e.message)+'</div>'; });
}
window.addEventListener('hashchange', route);

async function overview() {
  main.innerHTML = '<div class="top"><div><h1>Overview</h1><div class="sub">Licences issued, who holds them, and what happened lately.</div></div><a class="btn primary" href="#/issue">Issue a licence</a></div><div id="notice"></div><div class="cards" id="cards"></div><div class="two"><div class="panel"><div class="bar"><h2>Recent activity</h2></div><ul class="events" id="ev" style="padding:6px 16px"></ul></div><div class="panel"><div class="bar"><h2>Service</h2></div><div class="body kv" id="svc"></div></div></div>';
  const s = await api('/admin/api/stats');
  if (s.notice) $('#notice').innerHTML = '<div class="notice">'+esc(s.notice)+'</div>';
  $('#cards').innerHTML = [['Active licences', s.active, 'licences?status=active'],['Activated', s.activated, 'licences?status=activated'],['Never activated', s.unactivated, 'licences?status=unactivated'],['Installations', s.installations, 'reports'],['Seen this week', s.seen_7d, 'reports'],['Expiring in 30 days', s.expiring, 'licences?status=expiring'],['Expired', s.expired, 'licences?status=expired'],['Revoked', s.revoked, 'licences?status=revoked'],['Issued in 30 days', s.issued_30d, 'reports'],['All licences', s.licences, 'licences'],['People', s.users, 'people'],['Email', s.mail ? 'configured' : 'not set up', 'settings']]
    .map(([k,v,h]) => '<div class="card click" onclick="location.hash=\'#/'+h+'\'"><div class="k">'+k+'</div><div class="v">'+esc(v)+'</div></div>').join('');
  $('#ev').innerHTML = s.events.length ? s.events.map(e => '<li><time>'+esc(when(e.at))+'</time><b>'+esc(e.kind)+'</b> '+(e.licence_id?'<a href="#/licence/'+encodeURIComponent(e.licence_id)+'">'+esc(e.licensee||e.licence_id)+'</a>':'')+' <span style="color:var(--ink-3)">'+esc(e.detail)+'</span></li>').join('') : '<li class="empty">Nothing yet.</li>';
  $('#svc').innerHTML = '<span class="k">Email</span><span>'+(s.mail?'<span class="pill ok">configured</span>':'<span class="pill warn">not configured</span> <a href="#/settings">set up</a>')+'</span><span class="k">Public requests</span><span>'+(s.requests?'<span class="pill ok">open</span>':'<span class="pill grey">closed</span>')+' <a href="/request" target="_blank">page</a></span>';
}

let sortState = { col: 'created_at', dir: -1 };
async function licences(params) {
  const q = params.q || '', st = params.status || '', kind = params.kind || '';
  main.innerHTML = crumbs(['Licences','licences']) + '<div class="top"><div><h1>Licences</h1><div class="sub">Every key issued. Click one for its details, key text and history.</div></div><div class="row"><button class="btn" id="csv">Export CSV</button><a class="btn primary" href="#/issue">Issue a licence</a></div></div>'
   + '<div class="panel"><div class="bar"><input type="search" id="q" placeholder="Search by name, email, id or notes…" value="'+esc(q)+'"><select id="status"><option value="">Any status</option><option value="active">Active</option><option value="expiring">Expiring in 30 days</option><option value="activated">Activated</option><option value="unactivated">Never activated</option><option value="overseats">Over seats</option><option value="expired">Expired</option><option value="revoked">Revoked</option></select><select id="kind"><option value="">Any kind</option><option value="person">Person</option><option value="org">Organisation</option></select><span id="count" class="hint"></span></div><table><thead><tr>'
   + [['licensee','Licensee'],['kind','Kind'],['seats','Seats'],['issued','Issued'],['expires','Expires'],['status','Status'],['installs','Installed'],['last_refresh','Last refresh'],['last_sent','Emailed']].map(([c,t]) => '<th class="sort" data-col="'+c+'">'+t+'<span class="dir"></span></th>').join('') + '</tr></thead><tbody id="rows"></tbody></table></div>';
  $('#status').value = st; $('#kind').value = kind;
  let rows = [], next = '', seq = 0, lastKey = null;
  const query = () => new URLSearchParams({q: $('#q').value, status: $('#status').value, kind: $('#kind').value});
  const page = cursor => api('/admin/api/licences?' + query() + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
  const render = () => {
    const col = sortState.col, dir = sortState.dir;
    const val = l => col === 'status' ? status(l)[1] : (l[col] == null ? '' : l[col]);
    const sorted = rows.slice().sort((a,b) => { const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
    document.querySelectorAll('th.sort').forEach(th => th.querySelector('.dir').textContent = th.dataset.col === col ? (dir > 0 ? '▲' : '▼') : '');
    $('#count').innerHTML = rows.length + ' shown' + (next ? ' <button class="btn sm" id="more">Load more</button>' : '');
    if ($('#more')) $('#more').onclick = async () => { $('#more').disabled = true; const d = await page(next); rows = rows.concat(d.licences); next = d.next_cursor || ''; render(); };
    $('#rows').innerHTML = sorted.length ? sorted.map(l => { const [c,t]=status(l); return '<tr class="click" data-id="'+esc(l.id)+'"><td><b>'+esc(l.licensee)+'</b><br><span class="hint">'+esc(l.email)+' · <span class="mono">'+esc(l.id)+'</span>'+(l.source==='request'?' · <span class="pill blue">self-service</span>':'')+'</span></td><td>'+esc(l.kind)+'</td><td>'+l.seats+'</td><td>'+esc(l.issued)+'</td><td>'+esc(l.expires||'never')+'</td><td><span class="pill '+c+'">'+t+'</span></td><td>'+installed(l)+'</td><td class="hint">'+esc(l.last_refresh?when(l.last_refresh)+' ('+l.refresh_count+')':'never')+'</td><td class="hint">'+esc(l.last_sent?when(l.last_sent):'—')+'</td></tr>'; }).join('') : '<tr><td colspan="9" class="empty">No licences match.</td></tr>';
    document.querySelectorAll('tr.click').forEach(r => r.onclick = () => location.hash = '#/licence/'+encodeURIComponent(r.dataset.id));
  };
  // The first page only, and not again for the same filters; a slower
  // earlier answer arriving after a newer one is dropped.
  const load = async () => { const p = query(); const key = String(p); if (key === lastKey) return; lastKey = key; history.replaceState(null, '', '#/licences?'+p); const mine = ++seq; const d = await page(''); if (mine !== seq) return; rows = d.licences; next = d.next_cursor || ''; render(); };
  const everything = async () => { let out = rows.slice(), c = next; while (c) { const d = await page(c); out = out.concat(d.licences); c = d.next_cursor || ''; } return out; };
  $('#q').oninput = debounce(load, 300); $('#status').onchange = load; $('#kind').onchange = load;
  document.querySelectorAll('th.sort').forEach(th => th.onclick = () => { if (sortState.col === th.dataset.col) sortState.dir *= -1; else sortState = {col: th.dataset.col, dir: 1}; render(); });
  // The export is every matching row, following the pages, not just what is on screen.
  $('#csv').onclick = async () => { $('#csv').disabled = true; try { csv((await everything()).map(l => ({...l, status: status(l)[1], features: l.features.join(' ')})), ['id','licensee','email','kind','seats','issued','expires','status','installs','first_activated','last_seen','grace_days','features','source','refresh_count','last_refresh','sent_count','last_sent','notes'], 'shellmate-licences.csv'); } finally { $('#csv').disabled = false; } };
  await load();
}

async function detail(id) {
  const d = await api('/admin/api/licences/'+encodeURIComponent(id)); const l = d.licence; const [c,t] = status(l);
  main.innerHTML = crumbs(['Licences','licences'], [l.licensee]) + '<div class="top"><div class="row">'+back('licences','Licences')+'<h1>'+esc(l.licensee)+' <span class="pill '+c+'">'+t+'</span></h1></div><div class="row"><button class="btn" id="send">Email the key</button>'+(l.user?'<a class="btn" href="#/person/'+encodeURIComponent(l.user_id)+'">Open person</a>':'')+'</div></div>'
   + '<div class="two"><div><div class="panel"><div class="bar"><h2>Details</h2></div><div class="body kv">'
   + '<span class="k">Id</span><span class="mono">'+esc(l.id)+'</span><span class="k">Kind</span><span>'+esc(l.kind)+'</span><span class="k">Email</span><span><span class="row"><input id="email" value="'+esc(l.email||'')+'" style="max-width:280px"><button class="btn sm" id="saveemail">Save</button></span></span><span class="k">Seats</span><span>'+l.seats+'</span><span class="k">Issued</span><span>'+esc(l.issued)+'</span><span class="k">Expires</span><span>'+esc(l.expires||'never')+' <span class="hint">('+l.grace_days+' day grace)</span></span><span class="k">Features</span><span>'+esc(l.features.join(', '))+'</span><span class="k">Source</span><span>'+esc(l.source||'admin')+'</span><span class="k">Refreshes</span><span>'+l.refresh_count+' · last '+esc(when(l.last_refresh))+(l.last_ip?' from '+esc(l.last_ip):'')+'</span><span class="k">Emailed</span><span>'+(l.sent_count||0)+' time(s) · last '+esc(when(l.last_sent))+'</span>'+(l.revoked?'<span class="k">Revoked</span><span style="color:var(--danger)">'+esc(l.revoked_reason||'no reason given')+'</span>':'')+'</div></div>'
   + '<div class="panel"><div class="bar"><h2>Licence key</h2><span class="hint">what the licensee pastes into ShellMate</span></div><div class="body"><div class="key">'+esc(l.token)+'</div><div class="row" style="margin-top:8px"><button class="btn sm" id="copy">Copy key</button><button class="btn sm" id="dl">Download .key file</button></div></div></div>'
   + '<div class="panel"><div class="bar"><h2>Installations</h2><span class="hint">'+(l.installs||0)+' of '+l.seats+' seat(s) in use</span></div>'+(d.activations.length ? '<table><thead><tr><th>Machine</th><th>User</th><th>Platform</th><th>Version</th><th>First seen</th><th>Last seen</th><th></th></tr></thead><tbody>'+d.activations.map(a => '<tr'+(a.removed_at?' style="opacity:.55"':'')+'><td><b>'+esc(a.hostname||a.machine_id)+'</b>'+(a.removed_at?' <span class="pill grey">removed '+esc(when(a.removed_at))+'</span>':'')+'</td><td>'+esc(a.user||'—')+'</td><td class="hint">'+esc(a.platform||'—')+'</td><td class="mono">'+esc(a.version||'?')+'</td><td class="hint">'+esc(when(a.first_seen))+'</td><td class="hint">'+esc(when(a.last_seen))+' ('+a.seen_count+')</td><td><button class="btn sm forget" data-m="'+esc(a.machine_id)+'" title="Free the seat; the copy re-registers at its next refresh if it is still there">Forget</button></td></tr>').join('')+'</tbody></table>' : '<div class="body hint">No copy of ShellMate has reported this key yet. It is recorded the moment the key is entered, and again at every refresh.</div>')+'</div>'
   + '<div class="panel"><div class="bar"><h2>Notes</h2></div><div class="body"><textarea id="notes" rows="3">'+esc(l.notes||'')+'</textarea><div class="row" style="margin-top:8px"><button class="btn sm" id="savenotes">Save notes</button></div></div></div></div>'
   + '<div><div class="panel"><div class="bar"><h2>Renew</h2></div><div class="body"><div class="row"><input type="date" id="renew" value="'+esc(l.expires||'')+'" style="max-width:180px"><input type="number" id="seats" min="1" value="'+l.seats+'" style="max-width:100px" title="Seats"><label class="row" style="margin:0;font-weight:400"><input type="checkbox" id="renewsend" checked> email the new key</label>'+(l.revoked?'<label class="row" style="margin:0;font-weight:400"><input type="checkbox" id="renewrestore"> restore it too</label>':'')+'</div><div class="row" style="margin-top:10px"><button class="btn sm primary" id="dorenew">Renew and re-sign</button></div><p class="hint">Keeps the id and signs a new key with the new expiry; a blank date makes it perpetual. A copy of ShellMate holding the old key picks the new one up at its next refresh — no re-entry needed.'+(l.revoked?' This licence is revoked and stays revoked unless <i>restore it too</i> is ticked.':'')+'</p></div></div>'
   + '<div class="panel"><div class="bar"><h2>'+(l.revoked?'Restore':'Revoke')+'</h2></div><div class="body"><div class="row">'+(l.revoked?'<button class="btn sm" id="restore">Restore this licence</button>':'<input id="reason" placeholder="Reason (shown to the licensee)" style="max-width:320px"><button class="btn sm danger" id="revoke">Revoke</button>')+'</div></div></div>'
   + '<div class="panel"><div class="bar"><h2>History</h2></div><ul class="events" style="padding:6px 16px">'+(d.events.map(e => '<li><time>'+esc(when(e.at))+'</time><b>'+esc(e.kind)+'</b> <span style="color:var(--ink-3)">'+esc(e.detail)+'</span></li>').join('')||'<li class="hint">Nothing yet.</li>')+'</ul></div>'
   + '<button class="btn sm danger" id="del">Delete this licence record</button></div></div>';
  $('#copy').onclick = () => navigator.clipboard.writeText(l.token).then(() => toast('Key copied'));
  document.querySelectorAll('.forget').forEach(b => b.onclick = async () => { if (!confirm('Forget this installation? Its seat is freed; the copy re-registers itself at its next refresh if it still has the key.')) return; await api('/admin/api/licences/'+encodeURIComponent(id)+'/activations/'+encodeURIComponent(b.dataset.m), {method:'DELETE'}); toast('Forgotten'); route(); });
  $('#dl').onclick = () => { const a = document.createElement('a'); a.href = 'data:text/plain,'+encodeURIComponent(l.token+'\n'); a.download = 'shellmate-'+l.id+'.key'; a.click(); };
  $('#savenotes').onclick = async () => { await api('/admin/api/licences/'+encodeURIComponent(id), {method:'PUT', body:{notes: $('#notes').value}}); toast('Notes saved'); };
  $('#saveemail').onclick = async () => { await api('/admin/api/licences/'+encodeURIComponent(id), {method:'PUT', body:{notes: $('#notes').value, email: $('#email').value}}); toast('Email saved'); };
  $('#send').onclick = async () => { $('#send').disabled = true; try { await api('/admin/api/licences/'+encodeURIComponent(id)+'/send', {method:'POST'}); toast('Key emailed to '+l.email); route(); } catch (e) { toast(e.message); $('#send').disabled = false; } };
  $('#dorenew').onclick = async () => { try { const r = await api('/admin/api/licences/'+encodeURIComponent(id)+'/renew', {method:'POST', body:{expires: $('#renew').value, seats: $('#seats').value, send: $('#renewsend').checked, restore: !!($('#renewrestore') && $('#renewrestore').checked)}}); toast('Renewed'+(r.mailed?' and emailed':r.mail_error?' — email failed: '+r.mail_error:'')); route(); } catch (e) { toast(e.message); } };
  if ($('#revoke')) $('#revoke').onclick = async () => { if (!confirm('Revoke this licence? Its copy of ShellMate stops updating at its next refresh.')) return; await api('/admin/api/licences/'+encodeURIComponent(id)+'/revoke', {method:'POST', body:{reason: $('#reason').value}}); toast('Revoked'); route(); };
  if ($('#restore')) $('#restore').onclick = async () => { await api('/admin/api/licences/'+encodeURIComponent(id)+'/restore', {method:'POST'}); toast('Restored'); route(); };
  $('#del').onclick = async () => { if (!confirm('Delete the record entirely? The key stops verifying with the service and cannot be restored.')) return; await api('/admin/api/licences/'+encodeURIComponent(id), {method:'DELETE'}); toast('Deleted'); location.hash = '#/licences'; };
}

async function people(params) {
  main.innerHTML = crumbs(['People','people']) + '<div class="top"><div><h1>People</h1><div class="sub">Who holds licences. A person is created automatically when a licence is issued with an email.</div></div><div class="row"><button class="btn" id="csv">Export CSV</button><a class="btn primary" href="#/person/new">Add a person</a></div></div><div class="panel"><div class="bar"><input type="search" id="q" placeholder="Search by name, email or organisation…" value="'+esc(params.q||'')+'"><span id="count" class="hint"></span></div><table><thead><tr><th>Name</th><th>Email</th><th>Organisation</th><th>Licences</th><th>Active</th><th>Added</th></tr></thead><tbody id="rows"></tbody></table></div>';
  let rows = [];
  const load = async () => { rows = (await api('/admin/api/users?q='+encodeURIComponent($('#q').value))).users; $('#count').textContent = rows.length + ' shown'; $('#rows').innerHTML = rows.length ? rows.map(u => '<tr class="click" data-id="'+esc(u.id)+'"><td><b>'+esc(u.name)+'</b></td><td>'+esc(u.email)+'</td><td>'+esc(u.org)+'</td><td>'+u.licences+'</td><td>'+u.active+'</td><td class="hint">'+esc(when(u.created_at))+'</td></tr>').join('') : '<tr><td colspan="6" class="empty">Nobody yet.</td></tr>'; document.querySelectorAll('tr.click').forEach(r => r.onclick = () => location.hash = '#/person/'+encodeURIComponent(r.dataset.id)); };
  $('#q').oninput = debounce(load, 250); $('#csv').onclick = () => csv(rows, ['id','name','email','org','licences','active','notes','created_at'], 'shellmate-people.csv'); await load();
}
async function person(id) {
  const isNew = id === 'new';
  const d = isNew ? {user:{name:'',email:'',org:'',notes:''}, licences:[]} : await api('/admin/api/users/'+encodeURIComponent(id)); const u = d.user;
  main.innerHTML = crumbs(['People','people'], [isNew?'New person':u.name]) + '<div class="top"><div class="row">'+back('people','People')+'<h1>'+(isNew?'New person':esc(u.name))+'</h1></div>'+(isNew?'':'<a class="btn primary" href="#/issue?user='+encodeURIComponent(id)+'">Issue a licence to them</a>')+'</div>'
   + '<div class="two"><div class="panel"><div class="bar"><h2>Details</h2></div><div class="body form"><div><label>Name</label><input id="name" value="'+esc(u.name)+'"></div><div><label>Email</label><input id="email" value="'+esc(u.email)+'"></div><div class="full"><label>Organisation</label><input id="org" value="'+esc(u.org)+'"></div><div class="full"><label>Notes</label><textarea id="notes" rows="3">'+esc(u.notes)+'</textarea></div><div class="full row"><button class="btn primary" id="save">Save</button><span class="grow"></span>'+(isNew?'':'<button class="btn danger" id="del">Delete</button>')+'</div></div></div>'
   + (isNew ? '' : '<div class="panel"><div class="bar"><h2>Licences</h2></div><table><tbody>'+(d.licences.map(l => { const [c,t]=status(l); return '<tr class="click" data-id="'+esc(l.id)+'"><td>'+esc(l.kind)+' · '+l.seats+' seat(s)</td><td>'+esc(l.expires||'never')+'</td><td><span class="pill '+c+'">'+t+'</span></td></tr>'; }).join('')||'<tr><td class="empty">None yet.</td></tr>')+'</tbody></table></div>')+'</div>';
  document.querySelectorAll('tr.click').forEach(r => r.onclick = () => location.hash = '#/licence/'+encodeURIComponent(r.dataset.id));
  $('#save').onclick = async () => { const body = {name: $('#name').value, email: $('#email').value, org: $('#org').value, notes: $('#notes').value}; try { const r = await api(isNew ? '/admin/api/users' : '/admin/api/users/'+encodeURIComponent(id), {method: isNew?'POST':'PUT', body}); toast('Saved'); location.hash = '#/person/'+encodeURIComponent(r.user.id); if (!isNew) route(); } catch (e) { toast(e.message); } };
  if (!isNew) $('#del').onclick = async () => { if (!confirm('Delete this person? Their licences are kept but no longer linked.')) return; await api('/admin/api/users/'+encodeURIComponent(id), {method:'DELETE'}); location.hash = '#/people'; };
}

async function issue(params) {
  let p = {};
  if (params.user) { try { p = (await api('/admin/api/users/'+encodeURIComponent(params.user))).user; } catch (_) {} }
  const s = await api('/admin/api/stats');
  main.innerHTML = crumbs(['Issue a licence','issue']) + '<div class="top"><div><h1>Issue a licence</h1><div class="sub">Signed here, verified offline in ShellMate. '+(s.mail?'The key is emailed to the licensee as it is made.':'Email is not configured, so the key is shown here to copy — <a href="#/settings">set up email</a>.')+'</div></div></div><div class="panel"><div class="body form">'
   + '<div><label>Kind</label><select id="kind"><option value="person">Person</option><option value="org">Organisation</option></select></div><div><label>Seats</label><input type="number" id="seats" min="1" value="1"><div class="hint">People: 1. Organisations: how many engineers.</div></div>'
   + '<div><label>Licensee</label><input id="licensee" placeholder="Name or organisation" value="'+esc(p.org||p.name||'')+'"></div><div><label>Email</label><input id="email" placeholder="Where the key goes" value="'+esc(p.email||'')+'"></div>'
   + '<div><label>Expires</label><input type="date" id="expires"><div class="hint">Leave blank for a perpetual key.</div></div><div><label>Grace period (days)</label><input type="number" id="grace" min="0" max="365" value="14"><div class="hint">How long an expired key keeps working while a renewal is confirmed.</div></div>'
   + '<div class="full"><label>Notes</label><input id="notes" placeholder="Invoice number, reseller, anything you will want later"></div>'
   + '<div class="full row"><button class="btn primary" id="go">Issue and sign</button><label class="row" style="margin:0;font-weight:400"><input type="checkbox" id="send" '+(s.mail?'checked':'disabled')+'> email the key</label><label class="row" style="margin:0;font-weight:400"><input type="checkbox" id="mkuser" checked> add to People if new</label></div></div><div id="out"></div></div>';
  if (p.org) $('#kind').value = 'org';
  const d = new Date(); d.setFullYear(d.getFullYear()+1); $('#expires').value = d.toISOString().slice(0,10);
  $('#go').onclick = async () => { const body = {kind: $('#kind').value, seats: $('#seats').value, licensee: $('#licensee').value, email: $('#email').value, expires: $('#expires').value, grace_days: $('#grace').value, notes: $('#notes').value, create_user: $('#mkuser').checked, user_id: p.id||'', send: $('#send').checked}; $('#go').disabled = true; try { const r = await api('/admin/api/licences', {method:'POST', body}); const l = r.licence; $('#out').innerHTML = '<div class="body"><h3>Issued to '+esc(l.licensee)+' <span class="pill ok">'+esc(l.id)+'</span>'+(r.mailed?' <span class="pill blue">emailed</span>':'')+'</h3>'+(r.mail_error?'<p class="hint" style="color:var(--danger)">Email failed: '+esc(r.mail_error)+'</p>':'')+'<p class="hint">'+(r.mailed?'Sent to '+esc(l.email)+'. ':'')+'They paste it under Settings → Licence in ShellMate.</p><div class="key">'+esc(l.token)+'</div><div class="row" style="margin-top:10px"><button class="btn sm" id="copy">Copy key</button><a class="btn sm" href="#/licence/'+encodeURIComponent(l.id)+'">Open the record</a></div></div>'; $('#copy').onclick = () => navigator.clipboard.writeText(l.token).then(()=>toast('Key copied')); toast('Licence issued'); } catch (e) { toast(e.message); } finally { $('#go').disabled = false; } };
}

async function reports() {
  const r = await api('/admin/api/reports');
  const max = Math.max(1, ...r.by_month.map(m => m.n));
  main.innerHTML = crumbs(['Reports','reports']) + '<div class="top"><div><h1>Reports</h1><div class="sub">What is in force, what is running out, and how issuing has gone.</div></div><button class="btn" id="csv">Export renewals CSV</button></div>'
   + '<div class="cards">'+[['Seats in force', r.seats_in_force],['Expiring in 30 days', r.buckets.d30],['31–60 days', r.buckets.d60],['61–90 days', r.buckets.d90]].map(([k,v]) => '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join('')+'</div>'
   + '<div class="two"><div class="panel"><div class="bar"><h2>Issued per month</h2></div><div class="body"><div class="bars" style="margin-bottom:22px">'+(r.by_month.map(m => '<div class="b" style="height:'+Math.round(100*m.n/max)+'%"><span>'+m.n+'</span><i>'+esc(m.month.slice(2))+'</i></div>').join('')||'<div class="empty">Nothing issued yet.</div>')+'</div></div></div>'
   + '<div class="panel"><div class="bar"><h2>In force, by kind</h2></div><table><thead><tr><th>Kind</th><th>Licences</th><th>Seats</th></tr></thead><tbody>'+(r.by_kind.map(k => '<tr><td>'+esc(k.kind)+'</td><td>'+k.n+'</td><td>'+k.seats+'</td></tr>').join('')||'<tr><td colspan="3" class="empty">None.</td></tr>')+'</tbody></table><div class="bar"><h2>By source</h2></div><table><tbody>'+(r.by_source.map(k => '<tr><td>'+esc(k.source)+'</td><td>'+k.n+'</td></tr>').join('')||'<tr><td class="empty">None.</td></tr>')+'</tbody></table></div></div>'
   + '<div class="panel"><div class="bar"><h2>Renewals due in the next 90 days</h2><span class="hint">'+r.expiring.length+'</span></div><table><thead><tr><th>Licensee</th><th>Email</th><th>Kind</th><th>Seats</th><th>Expires</th><th>Last refresh</th></tr></thead><tbody>'+(r.expiring.map(l => '<tr class="click" data-id="'+esc(l.id)+'"><td><b>'+esc(l.licensee)+'</b></td><td>'+esc(l.email)+'</td><td>'+esc(l.kind)+'</td><td>'+l.seats+'</td><td>'+esc(l.expires)+'</td><td class="hint">'+esc(l.last_refresh?when(l.last_refresh):'never')+'</td></tr>').join('')||'<tr><td colspan="6" class="empty">Nothing due.</td></tr>')+'</tbody></table></div>';
  main.insertAdjacentHTML('beforeend', '<div class="two"><div class="panel"><div class="bar"><h2>Versions in use</h2><span class="hint">installations that have reported</span></div><table><thead><tr><th>ShellMate</th><th>Installations</th></tr></thead><tbody>'+(r.versions.map(v => '<tr><td class="mono">'+esc(v.version||'?')+'</td><td>'+v.n+'</td></tr>').join('')||'<tr><td colspan="2" class="empty">Nothing has reported yet.</td></tr>')+'</tbody></table></div>'
   + '<div class="panel"><div class="bar"><h2>More installations than seats</h2><span class="hint">'+r.over_seats.length+'</span></div><table><thead><tr><th>Licensee</th><th>Seats</th><th>Installed</th></tr></thead><tbody>'+(r.over_seats.map(l => '<tr class="click" data-id="'+esc(l.id)+'"><td><b>'+esc(l.licensee)+'</b><br><span class="hint">'+esc(l.email)+'</span></td><td>'+l.seats+'</td><td>'+installed(l)+'</td></tr>').join('')||'<tr><td colspan="3" class="empty">None.</td></tr>')+'</tbody></table></div></div>'
   + '<div class="panel"><div class="bar"><h2>Issued over a week ago, never activated</h2><span class="hint">'+r.unactivated.length+'</span></div><table><thead><tr><th>Licensee</th><th>Email</th><th>Kind</th><th>Issued</th><th>Emailed</th></tr></thead><tbody>'+(r.unactivated.map(l => '<tr class="click" data-id="'+esc(l.id)+'"><td><b>'+esc(l.licensee)+'</b></td><td>'+esc(l.email)+'</td><td>'+esc(l.kind)+'</td><td>'+esc(l.issued)+'</td><td class="hint">'+esc(l.last_sent?when(l.last_sent):'never')+'</td></tr>').join('')||'<tr><td colspan="5" class="empty">Everyone has installed their key.</td></tr>')+'</tbody></table></div>');
  document.querySelectorAll('tr.click').forEach(t => t.onclick = () => location.hash = '#/licence/'+encodeURIComponent(t.dataset.id));
  $('#csv').onclick = () => csv(r.expiring, ['id','licensee','email','kind','seats','issued','expires','refresh_count','last_refresh'], 'shellmate-renewals-90d.csv');
}

async function settings() {
  const d = await api('/admin/api/settings'); const s = d.settings;
  main.innerHTML = crumbs(['Settings','settings']) + '<div class="top"><div><h1>Settings</h1><div class="sub">Email, the public request page, and what the portal says.</div></div></div>'
   + '<div class="two"><div><div class="panel"><div class="bar"><h2>Email</h2>'+(d.mail?'<span class="pill ok">configured</span>':'<span class="pill warn">not configured</span>')+'</div><div class="body form">'+(d.mail?'':'<div class="full notice">Keys are emailed through <a href="https://resend.com" target="_blank">Resend</a>. Create an account, verify the sending domain (foundry-ns.com), make an API key, then run <span class="mono">wrangler secret put RESEND_API_KEY</span> in relay/admin and redeploy. Until then keys are copied by hand.</div>')+'<div class="full"><label>From</label><input id="mail_from" value="'+esc(s.mail_from||'')+'"><div class="hint">Must be on the verified domain, e.g. ShellMate &lt;licences@foundry-ns.com&gt;.</div></div><div class="full"><label>Subject</label><input id="mail_subject" value="'+esc(s.mail_subject||'')+'"></div><div class="full"><label>Introduction</label><textarea id="mail_intro" rows="3">'+esc(s.mail_intro||'')+'</textarea><div class="hint">Above the key. The licence details, the .key attachment and the footer are added automatically.</div></div><div class="full row"><input id="testto" placeholder="Send a test to…" style="max-width:280px"><button class="btn sm" id="test" '+(d.mail?'':'disabled')+'>Send test</button></div></div></div></div>'
   + '<div><div class="panel"><div class="bar"><h2>Public request page</h2></div><div class="body form"><div class="full"><label class="row" style="font-weight:600"><input type="checkbox" id="requests_enabled" '+(s.requests_enabled==='1'?'checked':'')+'> Open — anyone can request a key at <a href="'+esc(d.request_url)+'" target="_blank">'+esc(d.request_url)+'</a></label><div class="hint">A key is issued and emailed on its own, one live key per address; a second request re-sends the existing one. Needs email configured. Rate-limited per address.</div></div><div><label>Valid for (days)</label><input type="number" id="request_days" min="1" max="3650" value="'+esc(s.request_days||'30')+'"></div><div><label>Kind</label><select id="request_kind"><option value="person" '+(s.request_kind!=='org'?'selected':'')+'>Person</option><option value="org" '+(s.request_kind==='org'?'selected':'')+'>Organisation</option></select></div></div></div>'
   + '<div class="panel"><div class="bar"><h2>Portal</h2></div><div class="body form"><div class="full"><label>Notice on the overview</label><input id="portal_notice" value="'+esc(s.portal_notice||'')+'" placeholder="e.g. Renewals for the Glasgow site are due in October"></div><div class="full"><label>Public key</label><div class="key">'+esc(d.public_key)+'</div><div class="hint">Baked into ShellMate. Rotating it invalidates every issued key.</div></div></div></div>'
   + '<div class="row"><button class="btn primary" id="save">Save settings</button></div></div></div>';
  $('#save').onclick = async () => { const body = {}; for (const k of ['mail_from','mail_subject','mail_intro','request_days','request_kind','portal_notice']) body[k] = $('#'+k).value; body.requests_enabled = $('#requests_enabled').checked; try { await api('/admin/api/settings', {method:'PUT', body}); toast('Settings saved'); } catch (e) { toast(e.message); } };
  $('#test').onclick = async () => { try { await api('/admin/api/mail/test', {method:'POST', body:{to: $('#testto').value}}); toast('Test sent'); } catch (e) { toast(e.message); } };
}
route();
`;
