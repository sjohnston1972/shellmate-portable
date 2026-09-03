/**
 * worker.js — The ShellMate licence service and its admin portal (#447).
 *
 * One Cloudflare Worker, two faces:
 *
 *   /licence/refresh, /licence/check   what the application calls
 *   /  and /admin/*                    the portal for the person who
 *                                       issues, renews and revokes keys
 *
 * Keys are Ed25519-signed tokens (see backend/licence.py for the format).
 * The private key is a Worker secret and never leaves here; the public
 * half ships inside ShellMate. Records live in D1. The portal is a single
 * page served by this Worker, styled after workspace.foundry-ns.com, and
 * protected by a password (secret) exchanged for an HMAC session cookie.
 *
 * Everything from the network is treated as hostile: inputs are bounded
 * and typed, the admin API needs the cookie, logins and refreshes are
 * rate-limited per IP, and the application endpoints never return anything
 * but the one key they were asked about.
 *
 * Secrets (wrangler secret put): SIGNING_KEY_PKCS8_B64, ADMIN_PASSWORD,
 * SESSION_SECRET. Vars: PUBLIC_KEY_B64, PORTAL_TITLE.
 */

const SESSION_COOKIE = 'sma_session';
const SESSION_HOURS = 12;
const MAX = { name: 120, email: 200, org: 120, notes: 2000, reason: 300, seats: 100000 };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';
    try {
      if (path === '/health') return json(200, { ok: true, service: 'shellmate-admin' });
      if (path.startsWith('/licence/')) return application(request, env, path);
      if (path === '/admin/login' && request.method === 'POST') return login(request, env);
      if (path === '/admin/logout') return logout();
      if (path.startsWith('/admin/api/')) {
        if (!(await authed(request, env))) return json(401, { detail: 'Sign in first.' });
        return adminApi(request, env, path);
      }
      if (path === '/' || path === '/admin') {
        return html(await authed(request, env) ? portalPage(env) : loginPage(env));
      }
      return json(404, { detail: 'Not found.' });
    } catch (err) {
      console.error(err && err.stack || err);
      return json(500, { detail: 'The service hit an error. It has been logged.' });
    }
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
function isoDate(value) {
  const text = clean(value, 32);
  if (!text) return '';
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text) || Number.isNaN(Date.parse(text))) throw new Error('Dates are YYYY-MM-DD.');
  return text;
}
function today() { return new Date().toISOString().slice(0, 10); }
function newId(prefix) {
  const raw = new Uint8Array(6); crypto.getRandomValues(raw);
  return prefix + '-' + [...raw].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function rateLimited(env, request, bucket) {
  if (!env.RATE_LIMITER) return false;
  const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
  const { success } = await env.RATE_LIMITER.limit({ key: `${bucket}:${ip}` });
  return !success;
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
async function hmac(env, text) {
  const key = await crypto.subtle.importKey('raw', enc.encode(env.SESSION_SECRET || 'unset'),
                                            { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return b64url(await crypto.subtle.sign('HMAC', key, enc.encode(text)));
}
async function makeSession(env) {
  const nonce = new Uint8Array(16); crypto.getRandomValues(nonce);
  const body = `${Date.now() + SESSION_HOURS * 3600 * 1000}.${b64url(nonce)}`;
  return `${body}.${await hmac(env, body)}`;
}
async function authed(request, env) {
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
  let body = {};
  try { body = await request.json(); } catch (_) { return json(400, { detail: 'JSON expected.' }); }
  const given = String(body.password || '');
  const wanted = String(env.ADMIN_PASSWORD || '');
  if (!wanted || given.length !== wanted.length || !timingSafeEqual(given, wanted)) {
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
async function getLicence(env, id) {
  return env.DB.prepare('SELECT * FROM licences WHERE id = ?1').bind(id).first();
}
function publicRow(row) {
  if (!row) return null;
  const out = { ...row, revoked: !!row.revoked, features: JSON.parse(row.features || '["updates"]') };
  return out;
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
    if (row.revoked) return json(200, { id, revoked: true, reason: row.revoked_reason || '' });
    return json(200, { id, revoked: false, token: row.token, expires: row.expires || '' });
  }
  if (path === '/licence/check' && request.method === 'GET') {
    const id = clean(new URL(request.url).searchParams.get('id'), 64);
    const row = id ? await getLicence(env, id) : null;
    if (!row) return json(404, { detail: 'No such licence.' });
    return json(200, { id, kind: row.kind, expires: row.expires || '', revoked: !!row.revoked });
  }
  return json(404, { detail: 'Not found.' });
}

// ------------------------------------------------------------------ the admin API
async function adminApi(request, env, path) {
  const url = new URL(request.url);
  const method = request.method;
  const body = method === 'POST' || method === 'PUT' ? await request.json().catch(() => ({})) : {};

  if (path === '/admin/api/stats') {
    const total = await env.DB.prepare('SELECT COUNT(*) AS n FROM licences').first();
    const active = await env.DB.prepare("SELECT COUNT(*) AS n FROM licences WHERE revoked = 0 AND (expires = '' OR expires >= ?1)").bind(today()).first();
    const expiring = await env.DB.prepare("SELECT COUNT(*) AS n FROM licences WHERE revoked = 0 AND expires != '' AND expires >= ?1 AND expires <= ?2").bind(today(), addDays(30)).first();
    const users = await env.DB.prepare('SELECT COUNT(*) AS n FROM users').first();
    const recent = await env.DB.prepare('SELECT e.*, l.licensee FROM events e LEFT JOIN licences l ON l.id = e.licence_id ORDER BY e.at DESC LIMIT 12').all();
    return json(200, { licences: total.n, active: active.n, expiring: expiring.n, users: users.n, events: recent.results });
  }

  if (path === '/admin/api/licences' && method === 'GET') {
    const q = clean(url.searchParams.get('q'), 120).toLowerCase();
    const rows = q
      ? await env.DB.prepare('SELECT * FROM licences WHERE lower(licensee) LIKE ?1 OR lower(email) LIKE ?1 OR lower(id) LIKE ?1 OR lower(notes) LIKE ?1 ORDER BY created_at DESC LIMIT 500').bind(`%${q}%`).all()
      : await env.DB.prepare('SELECT * FROM licences ORDER BY created_at DESC LIMIT 500').all();
    return json(200, { licences: rows.results.map(publicRow) });
  }

  if (path === '/admin/api/licences' && method === 'POST') {
    const kind = body.kind === 'org' ? 'org' : 'person';
    const licensee = clean(body.licensee, MAX.name);
    if (!licensee) return json(400, { detail: 'A licensee name is needed.' });
    const seats = Math.min(MAX.seats, Math.max(1, parseInt(body.seats, 10) || 1));
    let expires, issued;
    try { expires = isoDate(body.expires); issued = isoDate(body.issued) || today(); }
    catch (err) { return json(400, { detail: err.message }); }
    const grace = Math.min(365, Math.max(0, parseInt(body.grace_days, 10) || 14));
    const features = Array.isArray(body.features) && body.features.length ? body.features.map(f => clean(f, 40)) : ['updates'];
    const email = clean(body.email, MAX.email);
    let userId = clean(body.user_id, 64) || null;
    if (!userId && email) {
      const existing = await env.DB.prepare('SELECT id FROM users WHERE lower(email) = lower(?1)').bind(email).first();
      if (existing) userId = existing.id;
      else if (body.create_user !== false) {
        userId = newId('usr');
        await env.DB.prepare('INSERT INTO users (id, name, email, org, notes, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)')
          .bind(userId, licensee, email, kind === 'org' ? licensee : clean(body.org, MAX.org), '', Date.now()).run();
      }
    }
    const id = newId(kind === 'org' ? 'org' : 'lic');
    const row = { id, kind, licensee, email, seats, issued, expires, grace_days: grace, features: JSON.stringify(features) };
    const token = await signToken(env, payloadFor(row));
    await env.DB.prepare(`INSERT INTO licences (id, user_id, kind, licensee, email, seats, issued, expires, grace_days, features, token, notes, created_at)
                          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)`)
      .bind(id, userId, kind, licensee, email, seats, issued, expires, grace, row.features, token, clean(body.notes, MAX.notes), Date.now()).run();
    await logEvent(env, id, 'issued', `${kind} · ${seats} seat(s) · expires ${expires || 'never'}`);
    return json(201, { licence: publicRow(await getLicence(env, id)) });
  }

  const m = path.match(/^\/admin\/api\/licences\/([^/]+)(?:\/(revoke|restore|renew|events))?$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    const row = await getLicence(env, id);
    if (!row) return json(404, { detail: 'No such licence.' });
    const action = m[2];
    if (!action && method === 'GET') {
      const events = await env.DB.prepare('SELECT * FROM events WHERE licence_id = ?1 ORDER BY at DESC LIMIT 50').bind(id).all();
      return json(200, { licence: publicRow(row), events: events.results });
    }
    if (!action && method === 'DELETE') {
      await env.DB.prepare('DELETE FROM licences WHERE id = ?1').bind(id).run();
      await logEvent(env, id, 'deleted', row.licensee);
      return json(200, { deleted: true });
    }
    if (!action && method === 'PUT') {
      const notes = clean(body.notes, MAX.notes);
      await env.DB.prepare('UPDATE licences SET notes = ?1 WHERE id = ?2').bind(notes, id).run();
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
      let expires;
      try { expires = isoDate(body.expires); } catch (err) { return json(400, { detail: err.message }); }
      const seats = body.seats ? Math.min(MAX.seats, Math.max(1, parseInt(body.seats, 10) || row.seats)) : row.seats;
      const updated = { ...row, expires, seats, issued: today() };
      const token = await signToken(env, payloadFor(updated));
      await env.DB.prepare('UPDATE licences SET expires = ?1, seats = ?2, issued = ?3, token = ?4, revoked = 0, revoked_reason = \'\' WHERE id = ?5')
        .bind(expires, seats, updated.issued, token, id).run();
      await logEvent(env, id, 'renewed', `expires ${expires || 'never'} · ${seats} seat(s)`);
      return json(200, { licence: publicRow(await getLicence(env, id)) });
    }
  }

  if (path === '/admin/api/users' && method === 'GET') {
    const q = clean(url.searchParams.get('q'), 120).toLowerCase();
    const rows = q
      ? await env.DB.prepare('SELECT u.*, (SELECT COUNT(*) FROM licences l WHERE l.user_id = u.id) AS licences FROM users u WHERE lower(name) LIKE ?1 OR lower(email) LIKE ?1 OR lower(org) LIKE ?1 ORDER BY created_at DESC LIMIT 500').bind(`%${q}%`).all()
      : await env.DB.prepare('SELECT u.*, (SELECT COUNT(*) FROM licences l WHERE l.user_id = u.id) AS licences FROM users u ORDER BY created_at DESC LIMIT 500').all();
    return json(200, { users: rows.results });
  }
  if (path === '/admin/api/users' && method === 'POST') {
    const name = clean(body.name, MAX.name);
    if (!name) return json(400, { detail: 'A name is needed.' });
    const id = newId('usr');
    await env.DB.prepare('INSERT INTO users (id, name, email, org, notes, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)')
      .bind(id, name, clean(body.email, MAX.email), clean(body.org, MAX.org), clean(body.notes, MAX.notes), Date.now()).run();
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
      await env.DB.prepare('UPDATE users SET name = ?1, email = ?2, org = ?3, notes = ?4 WHERE id = ?5')
        .bind(clean(body.name, MAX.name) || row.name, clean(body.email, MAX.email), clean(body.org, MAX.org), clean(body.notes, MAX.notes), id).run();
      return json(200, { user: await env.DB.prepare('SELECT * FROM users WHERE id = ?1').bind(id).first() });
    }
    if (method === 'DELETE') {
      await env.DB.prepare('DELETE FROM users WHERE id = ?1').bind(id).run();
      return json(200, { deleted: true });
    }
  }
  return json(404, { detail: 'Not found.' });
}
function addDays(n) { const d = new Date(); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); }

// ------------------------------------------------------------------ the portal
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
.side { background:#fff; border-right:1px solid var(--line); padding:20px 14px; display:flex; flex-direction:column; gap:4px }
.brand { display:flex; align-items:center; gap:10px; padding:4px 8px 18px; font-family:var(--font-display); font-weight:700; font-size:16px }
.brand .dot { width:28px; height:28px; border-radius:8px; background:linear-gradient(135deg,var(--accent),#7cd4ff); display:grid; place-items:center; color:#fff; font-size:13px }
.nav { display:flex; align-items:center; gap:10px; padding:9px 10px; border-radius:var(--radius-sm); color:var(--ink-2); cursor:pointer; font-weight:500 }
.nav:hover { background:var(--bg) } .nav.active { background:var(--accent-soft); color:#0369a1 }
.nav svg { width:16px; height:16px; stroke:currentColor; fill:none; stroke-width:2 }
.side .foot { margin-top:auto; font-size:12px; color:var(--ink-4); padding:8px 10px }
.main { padding:28px 32px; max-width:1180px }
.top { display:flex; align-items:center; justify-content:space-between; margin-bottom:22px; gap:16px }
.top .sub { color:var(--ink-3); margin-top:2px }
.btn { font:inherit; font-weight:600; padding:8px 14px; border-radius:var(--radius-sm); border:1px solid var(--line-2); background:#fff; color:var(--ink-2); cursor:pointer; display:inline-flex; align-items:center; gap:6px }
.btn:hover { background:var(--bg) } .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff } .btn.primary:hover { filter:brightness(.95) }
.btn.danger { color:var(--danger); border-color:#e8c4c1 } .btn.sm { padding:5px 10px; font-size:12px }
.btn:disabled { opacity:.5; cursor:default }
.cards { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:22px }
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px }
.card .k { color:var(--ink-3); font-size:12px; font-weight:500 } .card .v { font-family:var(--font-display); font-size:26px; font-weight:700; margin-top:4px }
.panel { background:#fff; border:1px solid var(--line); border-radius:var(--radius); overflow:hidden }
.panel .bar { display:flex; align-items:center; gap:10px; padding:12px 16px; border-bottom:1px solid var(--line) }
.panel .bar input { flex:1; max-width:360px }
input, select, textarea { font:inherit; padding:8px 10px; border:1px solid var(--line-2); border-radius:var(--radius-sm); background:#fff; color:var(--ink); width:100% }
input:focus, select:focus, textarea:focus { outline:2px solid var(--accent-soft); border-color:var(--accent) }
table { width:100%; border-collapse:collapse } th { text-align:left; font-size:12px; color:var(--ink-3); font-weight:600; padding:10px 16px; border-bottom:1px solid var(--line); background:var(--card) }
td { padding:11px 16px; border-bottom:1px solid var(--line); vertical-align:top } tr:hover td { background:#fafbfc } tr.click { cursor:pointer }
.pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600 }
.pill.ok { background:#e7f5ec; color:var(--ok) } .pill.warn { background:#fdf3e1; color:var(--warn) } .pill.bad { background:#fbe9e7; color:var(--danger) } .pill.grey { background:var(--bg); color:var(--ink-3) }
.form { display:grid; grid-template-columns:1fr 1fr; gap:14px 18px } .form .full { grid-column:1/-1 } label { display:block; font-size:12px; font-weight:600; color:var(--ink-2); margin-bottom:5px } .hint { font-size:12px; color:var(--ink-4); margin-top:4px }
.drawer { position:fixed; inset:0 0 0 auto; width:min(560px,100%); background:#fff; border-left:1px solid var(--line); box-shadow:-12px 0 32px rgba(15,23,42,.08); padding:24px 26px; overflow:auto; z-index:20 }
.scrim { position:fixed; inset:0; background:rgba(15,23,42,.25); z-index:19 }
.key { font-family:var(--font-mono); font-size:11px; word-break:break-all; background:var(--bg); border:1px solid var(--line); border-radius:var(--radius-sm); padding:10px; user-select:all }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap } .grow { flex:1 }
.kv { display:grid; grid-template-columns:130px 1fr; gap:6px 12px; font-size:13px } .kv .k { color:var(--ink-3) }
.events { list-style:none; padding:0; margin:0 } .events li { padding:8px 0; border-bottom:1px solid var(--line); font-size:13px } .events time { color:var(--ink-4); font-size:12px; margin-right:8px }
.toast { position:fixed; right:20px; bottom:20px; background:var(--ink); color:#fff; padding:10px 14px; border-radius:var(--radius-sm); font-size:13px; z-index:30 }
.login { min-height:100vh; display:grid; place-items:center } .login .card { width:360px; padding:28px }
.empty { padding:36px; text-align:center; color:var(--ink-4) }
@media (max-width:860px){ .app{grid-template-columns:1fr} .side{display:none} .cards{grid-template-columns:1fr 1fr} .form{grid-template-columns:1fr} }
`;
const FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">';

function loginPage(env) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</title>${FONTS}<style>${STYLE}</style></head>
<body><div class="login"><div class="card"><div class="brand"><span class="dot">S</span> ${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</div>
<form id="f"><label>Password</label><input type="password" id="p" autofocus autocomplete="current-password"><div style="height:14px"></div><button class="btn primary" style="width:100%;justify-content:center">Sign in</button><p class="hint" id="err"></p></form></div></div>
<script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const r=await fetch('/admin/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:document.getElementById('p').value})});if(r.ok)location.reload();else document.getElementById('err').textContent=(await r.json()).detail||'Wrong password.';};</script></body></html>`;
}

function portalPage(env) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</title>${FONTS}<style>${STYLE}</style></head>
<body><div class="app">
<aside class="side"><div class="brand"><span class="dot">S</span> ${esc(env.PORTAL_TITLE || 'ShellMate Admin')}</div>
<div class="nav active" data-view="overview"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>Overview</div>
<div class="nav" data-view="licences"><svg viewBox="0 0 24 24"><path d="M21 2l-2 2m-7.6 7.6a5 5 0 1 1-7 7 5 5 0 0 1 7-7zm0 0L19 3.5M15 5l2 2"/></svg>Licences</div>
<div class="nav" data-view="people"><svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>People</div>
<div class="nav" data-view="issue"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/></svg>Issue a licence</div>
<div class="foot">Public key<br><span class="mono">${esc((env.PUBLIC_KEY_B64 || '').slice(0, 20))}…</span><br><a href="/admin/logout">Sign out</a></div></aside>
<main class="main" id="main"></main></div>
<script>${PORTAL_JS}</script></body></html>`;
}
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

const PORTAL_JS = String.raw`
const $ = (s, r=document) => r.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async (path, opts={}) => { const r = await fetch(path, {headers:{'content-type':'application/json'}, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined}); const d = await r.json().catch(()=>({})); if (!r.ok) throw new Error(d.detail || ('HTTP '+r.status)); return d; };
const toast = m => { const t = document.createElement('div'); t.className='toast'; t.textContent=m; document.body.appendChild(t); setTimeout(()=>t.remove(), 2600); };
const when = ts => ts ? new Date(ts).toLocaleString() : '—';
const status = l => l.revoked ? ['bad','revoked'] : !l.expires ? ['ok','perpetual'] : (l.expires < today() ? ['bad','expired'] : (l.expires <= addDays(30) ? ['warn','expiring'] : ['ok','active']));
const today = () => new Date().toISOString().slice(0,10);
const addDays = n => { const d=new Date(); d.setDate(d.getDate()+n); return d.toISOString().slice(0,10); };
const main = $('#main');
document.querySelectorAll('.nav').forEach(n => n.onclick = () => go(n.dataset.view));
function go(view, arg) { document.querySelectorAll('.nav').forEach(n => n.classList.toggle('active', n.dataset.view===view)); ({overview, licences, people, issue})[view](arg); }

async function overview() {
  main.innerHTML = '<div class="top"><div><h1>Overview</h1><div class="sub">Licences issued, who holds them, and what happened lately.</div></div><button class="btn primary" onclick="go(\'issue\')">Issue a licence</button></div><div class="cards" id="cards"></div><div class="panel"><div class="bar"><h2>Recent activity</h2></div><ul class="events" id="ev" style="padding:6px 16px"></ul></div>';
  const s = await api('/admin/api/stats');
  $('#cards').innerHTML = [['Licences', s.licences],['Active', s.active],['Expiring in 30 days', s.expiring],['People', s.users]].map(([k,v]) => '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join('');
  $('#ev').innerHTML = s.events.length ? s.events.map(e => '<li><time>'+esc(when(e.at))+'</time><b>'+esc(e.kind)+'</b> '+esc(e.licensee||'')+' <span style="color:var(--ink-3)">'+esc(e.detail)+'</span></li>').join('') : '<li class="empty">Nothing yet.</li>';
}

async function licences(q='') {
  main.innerHTML = '<div class="top"><div><h1>Licences</h1><div class="sub">Every key issued. Click one for its details, key text and history.</div></div><button class="btn primary" onclick="go(\'issue\')">Issue a licence</button></div><div class="panel"><div class="bar"><input id="q" placeholder="Search by name, email, id or notes…" value="'+esc(q)+'"><span id="count" class="hint"></span></div><table><thead><tr><th>Licensee</th><th>Kind</th><th>Seats</th><th>Expires</th><th>Status</th><th>Last refresh</th></tr></thead><tbody id="rows"></tbody></table></div>';
  const load = async () => { const d = await api('/admin/api/licences?q='+encodeURIComponent($('#q').value)); $('#count').textContent = d.licences.length + ' shown'; $('#rows').innerHTML = d.licences.length ? d.licences.map(l => { const [c,t]=status(l); return '<tr class="click" data-id="'+esc(l.id)+'"><td><b>'+esc(l.licensee)+'</b><br><span class="hint">'+esc(l.email)+' · <span class="mono">'+esc(l.id)+'</span></span></td><td>'+esc(l.kind)+'</td><td>'+l.seats+'</td><td>'+esc(l.expires||'never')+'</td><td><span class="pill '+c+'">'+t+'</span></td><td class="hint">'+esc(l.last_refresh?when(l.last_refresh)+' ('+l.refresh_count+')':'never')+'</td></tr>'; }).join('') : '<tr><td colspan="6" class="empty">No licences match.</td></tr>'; document.querySelectorAll('tr.click').forEach(r => r.onclick = () => detail(r.dataset.id)); };
  $('#q').oninput = debounce(load, 250); await load();
}
function debounce(f, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; }

async function detail(id) {
  const d = await api('/admin/api/licences/'+encodeURIComponent(id)); const l = d.licence; const [c,t] = status(l);
  const scrim = document.createElement('div'); scrim.className='scrim'; const dr = document.createElement('div'); dr.className='drawer';
  dr.innerHTML = '<div class="row"><h2 class="grow">'+esc(l.licensee)+' <span class="pill '+c+'">'+t+'</span></h2><button class="btn sm" id="x">Close</button></div>'
   + '<div class="kv" style="margin:16px 0"><span class="k">Id</span><span class="mono">'+esc(l.id)+'</span><span class="k">Kind</span><span>'+esc(l.kind)+'</span><span class="k">Email</span><span>'+esc(l.email||'—')+'</span><span class="k">Seats</span><span>'+l.seats+'</span><span class="k">Issued</span><span>'+esc(l.issued)+'</span><span class="k">Expires</span><span>'+esc(l.expires||'never')+' <span class="hint">('+l.grace_days+' day grace)</span></span><span class="k">Features</span><span>'+esc(l.features.join(', '))+'</span><span class="k">Refreshes</span><span>'+l.refresh_count+' · last '+esc(when(l.last_refresh))+(l.last_ip?' from '+esc(l.last_ip):'')+'</span>'+(l.revoked?'<span class="k">Revoked</span><span style="color:var(--danger)">'+esc(l.revoked_reason||'no reason given')+'</span>':'')+'</div>'
   + '<label>Licence key <span class="hint">— what the licensee pastes into ShellMate</span></label><div class="key" id="key">'+esc(l.token)+'</div><div class="row" style="margin:8px 0 18px"><button class="btn sm" id="copy">Copy key</button><button class="btn sm" id="dl">Download .key file</button></div>'
   + '<label>Notes</label><textarea id="notes" rows="3">'+esc(l.notes||'')+'</textarea><div class="row" style="margin:8px 0 18px"><button class="btn sm" id="savenotes">Save notes</button></div>'
   + '<h3>Renew</h3><div class="row" style="margin:8px 0 18px"><input type="date" id="renew" value="'+esc(l.expires||'')+'" style="max-width:180px"><input type="number" id="seats" min="1" value="'+l.seats+'" style="max-width:100px" title="Seats"><button class="btn sm primary" id="dorenew">Renew and re-sign</button></div><p class="hint" style="margin-top:-12px">Renewing keeps the id, signs a new key with the new expiry, and the licensee\'s copy picks it up at its next refresh — no re-entry needed.</p>'
   + '<h3>'+(l.revoked?'Restore':'Revoke')+'</h3><div class="row" style="margin:8px 0 18px">'+(l.revoked?'<button class="btn sm" id="restore">Restore this licence</button>':'<input id="reason" placeholder="Reason (shown to the licensee)"><button class="btn sm danger" id="revoke">Revoke</button>')+'</div>'
   + '<h3>History</h3><ul class="events">'+(d.events.map(e => '<li><time>'+esc(when(e.at))+'</time><b>'+esc(e.kind)+'</b> <span style="color:var(--ink-3)">'+esc(e.detail)+'</span></li>').join('')||'<li class="hint">Nothing yet.</li>')+'</ul>'
   + '<div style="margin-top:22px"><button class="btn sm danger" id="del">Delete this licence record</button></div>';
  document.body.append(scrim, dr);
  const close = () => { scrim.remove(); dr.remove(); };
  scrim.onclick = close; $('#x', dr).onclick = close;
  $('#copy', dr).onclick = () => navigator.clipboard.writeText(l.token).then(() => toast('Key copied'));
  $('#dl', dr).onclick = () => { const a = document.createElement('a'); a.href = 'data:text/plain,'+encodeURIComponent(l.token+'\n'); a.download = 'shellmate-'+l.id+'.key'; a.click(); };
  $('#savenotes', dr).onclick = async () => { await api('/admin/api/licences/'+encodeURIComponent(id), {method:'PUT', body:{notes: $('#notes', dr).value}}); toast('Saved'); };
  $('#dorenew', dr).onclick = async () => { try { await api('/admin/api/licences/'+encodeURIComponent(id)+'/renew', {method:'POST', body:{expires: $('#renew', dr).value, seats: $('#seats', dr).value}}); toast('Renewed'); close(); detail(id); } catch (e) { toast(e.message); } };
  if ($('#revoke', dr)) $('#revoke', dr).onclick = async () => { if (!confirm('Revoke this licence? Its copy of ShellMate stops updating at its next refresh.')) return; await api('/admin/api/licences/'+encodeURIComponent(id)+'/revoke', {method:'POST', body:{reason: $('#reason', dr).value}}); toast('Revoked'); close(); detail(id); };
  if ($('#restore', dr)) $('#restore', dr).onclick = async () => { await api('/admin/api/licences/'+encodeURIComponent(id)+'/restore', {method:'POST'}); toast('Restored'); close(); detail(id); };
  $('#del', dr).onclick = async () => { if (!confirm('Delete the record entirely? The key stops verifying with the service and cannot be restored.')) return; await api('/admin/api/licences/'+encodeURIComponent(id), {method:'DELETE'}); toast('Deleted'); close(); go('licences'); };
}

async function people() {
  main.innerHTML = '<div class="top"><div><h1>People</h1><div class="sub">Who holds licences. A person is created automatically when a licence is issued with an email.</div></div><button class="btn" id="add">Add a person</button></div><div class="panel"><div class="bar"><input id="q" placeholder="Search by name, email or organisation…"></div><table><thead><tr><th>Name</th><th>Email</th><th>Organisation</th><th>Licences</th><th>Added</th></tr></thead><tbody id="rows"></tbody></table></div>';
  const load = async () => { const d = await api('/admin/api/users?q='+encodeURIComponent($('#q').value)); $('#rows').innerHTML = d.users.length ? d.users.map(u => '<tr class="click" data-id="'+esc(u.id)+'"><td><b>'+esc(u.name)+'</b></td><td>'+esc(u.email)+'</td><td>'+esc(u.org)+'</td><td>'+u.licences+'</td><td class="hint">'+esc(when(u.created_at))+'</td></tr>').join('') : '<tr><td colspan="5" class="empty">Nobody yet.</td></tr>'; document.querySelectorAll('tr.click').forEach(r => r.onclick = () => person(r.dataset.id)); };
  $('#q').oninput = debounce(load, 250); $('#add').onclick = () => person(null); await load();
}
async function person(id) {
  const d = id ? await api('/admin/api/users/'+encodeURIComponent(id)) : {user:{name:'',email:'',org:'',notes:''}, licences:[]}; const u = d.user;
  const scrim = document.createElement('div'); scrim.className='scrim'; const dr = document.createElement('div'); dr.className='drawer';
  dr.innerHTML = '<div class="row"><h2 class="grow">'+(id?esc(u.name):'New person')+'</h2><button class="btn sm" id="x">Close</button></div><div class="form" style="margin:16px 0"><div><label>Name</label><input id="name" value="'+esc(u.name)+'"></div><div><label>Email</label><input id="email" value="'+esc(u.email)+'"></div><div class="full"><label>Organisation</label><input id="org" value="'+esc(u.org)+'"></div><div class="full"><label>Notes</label><textarea id="notes" rows="3">'+esc(u.notes)+'</textarea></div></div><div class="row"><button class="btn primary" id="save">Save</button>'+(id?'<button class="btn" id="issue">Issue a licence to them</button><span class="grow"></span><button class="btn danger" id="del">Delete</button>':'')+'</div>'
   + (id ? '<h3 style="margin-top:22px">Licences</h3><table style="margin-top:8px"><tbody>'+(d.licences.map(l => { const [c,t]=status(l); return '<tr class="click" data-id="'+esc(l.id)+'"><td>'+esc(l.kind)+' · '+l.seats+' seat(s)</td><td>'+esc(l.expires||'never')+'</td><td><span class="pill '+c+'">'+t+'</span></td></tr>'; }).join('')||'<tr><td class="hint">None yet.</td></tr>')+'</tbody></table>' : '');
  document.body.append(scrim, dr); const close = () => { scrim.remove(); dr.remove(); }; scrim.onclick = close; $('#x', dr).onclick = close;
  dr.querySelectorAll('tr.click').forEach(r => r.onclick = () => { close(); detail(r.dataset.id); });
  $('#save', dr).onclick = async () => { const body = {name: $('#name',dr).value, email: $('#email',dr).value, org: $('#org',dr).value, notes: $('#notes',dr).value}; try { await api(id ? '/admin/api/users/'+encodeURIComponent(id) : '/admin/api/users', {method: id?'PUT':'POST', body}); toast('Saved'); close(); go('people'); } catch (e) { toast(e.message); } };
  if (id) { $('#issue', dr).onclick = () => { close(); go('issue', u); }; $('#del', dr).onclick = async () => { if (!confirm('Delete this person? Their licences are kept but no longer linked.')) return; await api('/admin/api/users/'+encodeURIComponent(id), {method:'DELETE'}); close(); go('people'); }; }
}

async function issue(prefill) {
  const p = prefill || {};
  main.innerHTML = '<div class="top"><div><h1>Issue a licence</h1><div class="sub">Signed here, verified offline in ShellMate. The key is shown once it is made; it can always be reopened from the list.</div></div></div><div class="panel" style="padding:20px"><div class="form">'
   + '<div><label>Kind</label><select id="kind"><option value="person">Person</option><option value="org">Organisation</option></select></div><div><label>Seats</label><input type="number" id="seats" min="1" value="1"><div class="hint">People: 1. Organisations: how many engineers.</div></div>'
   + '<div><label>Licensee</label><input id="licensee" placeholder="Name or organisation" value="'+esc(p.org||p.name||'')+'"></div><div><label>Email</label><input id="email" placeholder="Where the key goes" value="'+esc(p.email||'')+'"></div>'
   + '<div><label>Expires</label><input type="date" id="expires"><div class="hint">Leave blank for a perpetual key.</div></div><div><label>Grace period (days)</label><input type="number" id="grace" min="0" max="365" value="14"><div class="hint">How long an expired key keeps working while a renewal is confirmed.</div></div>'
   + '<div class="full"><label>Notes</label><input id="notes" placeholder="Invoice number, reseller, anything you will want later"></div>'
   + '<div class="full row"><button class="btn primary" id="go">Issue and sign</button><label class="row" style="margin:0;font-weight:400"><input type="checkbox" id="mkuser" checked style="width:auto"> add to People if the email is new</label></div></div><div id="out"></div></div>';
  if (p.org) $('#kind').value = 'org';
  const q = $('#expires'); const d = new Date(); d.setFullYear(d.getFullYear()+1); q.value = d.toISOString().slice(0,10);
  $('#go').onclick = async () => { const body = {kind: $('#kind').value, seats: $('#seats').value, licensee: $('#licensee').value, email: $('#email').value, expires: $('#expires').value, grace_days: $('#grace').value, notes: $('#notes').value, create_user: $('#mkuser').checked, user_id: p.id||''}; $('#go').disabled = true; try { const r = await api('/admin/api/licences', {method:'POST', body}); const l = r.licence; $('#out').innerHTML = '<div style="margin-top:22px"><h3>Issued to '+esc(l.licensee)+' <span class="pill ok">'+esc(l.id)+'</span></h3><p class="hint">Send them this key. They paste it under Settings → Licence in ShellMate.</p><div class="key">'+esc(l.token)+'</div><div class="row" style="margin-top:10px"><button class="btn sm" id="copy">Copy key</button><button class="btn sm" id="open">Open the record</button></div></div>'; $('#copy').onclick = () => navigator.clipboard.writeText(l.token).then(()=>toast('Key copied')); $('#open').onclick = () => detail(l.id); toast('Licence issued'); } catch (e) { toast(e.message); } finally { $('#go').disabled = false; } };
}
go('overview');
`;
