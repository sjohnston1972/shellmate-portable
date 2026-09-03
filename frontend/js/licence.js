/**
 * licence.js — The Licence section in Settings (#446, #448).
 *
 * Paste or import a key, see who it is for and until when, refresh it
 * against the service, or remove it. The key is verified server-side
 * against the public key inside the executable; nothing here decides
 * validity. ShellMate works without a key; what the key buys is updating
 * from inside the application, and the section says so.
 */
(function () {
  'use strict';

  let statusEl, detailEl, keyInput, fileInput;

  document.addEventListener('DOMContentLoaded', () => {
    statusEl = document.getElementById('licence-status');
    detailEl = document.getElementById('licence-detail');
    keyInput = document.getElementById('licence-key-input');
    fileInput = document.getElementById('licence-file-input');
    if (!statusEl) return;
    document.getElementById('licence-install').addEventListener('click', install);
    document.getElementById('licence-import').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', importFile);
    document.getElementById('licence-refresh').addEventListener('click', refresh);
    document.getElementById('licence-remove').addEventListener('click', remove);
    window.addEventListener('shellmate:settings-opened', load);
    load();
  });

  async function load() {
    try {
      render(await (await fetch('/api/licence')).json());
    } catch (_) { /* the section shows its last state */ }
  }

  function render(s) {
    if (!statusEl) return;
    const pill = { active: 'ok', grace: 'warn', expired: 'bad', revoked: 'bad', none: 'grey' }[s.state] || 'grey';
    statusEl.className = 'licence-pill licence-' + pill;
    statusEl.textContent = { active: 'Licensed', grace: 'Grace period', expired: 'Expired',
                             revoked: 'Revoked', none: 'No licence' }[s.state] || s.state;
    detailEl.textContent = s.detail || '';
    const lic = s.licence;
    const grid = document.getElementById('licence-grid');
    grid.innerHTML = '';
    if (lic) {
      const rows = [
        ['Licensee', lic.licensee + (lic.email ? ` <${lic.email}>` : '')],
        ['Kind', lic.kind === 'org' ? `Organisation, ${lic.seats} seat${lic.seats === 1 ? '' : 's'}` : 'Person'],
        ['Issued', lic.issued || '—'],
        ['Expires', lic.expires || 'never'],
        ['Grace period', `${lic.grace_days} days`],
        ['Covers', (lic.features || []).join(', ')],
        ['Key id', lic.id],
        ['Last refreshed', s.refreshed_at ? new Date(s.refreshed_at * 1000).toLocaleString() : 'never'],
      ];
      rows.forEach(([k, v]) => {
        const key = document.createElement('span'); key.className = 'licence-k'; key.textContent = k;
        const val = document.createElement('span'); val.className = 'licence-v'; val.textContent = v;
        grid.append(key, val);
      });
    }
    document.getElementById('licence-have').classList.toggle('hidden', !lic);
    document.getElementById('licence-none').classList.toggle('hidden', !!lic);
  }

  async function post(path, body) {
    const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    return data;
  }

  async function install() {
    const key = (keyInput.value || '').trim();
    if (!key) { note('Paste the key first.', true); return; }
    try {
      const s = await post('/api/licence', { key });
      keyInput.value = '';
      render(s);
      note('Licence installed.');
    } catch (e) {
      note(e.message, true);
    }
  }

  function importFile(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { keyInput.value = String(reader.result || '').trim(); install(); };
    reader.readAsText(file);
  }

  async function refresh() {
    try {
      const s = await post('/api/licence/refresh');
      render(s);
      note({ renewed: 'Renewed by the licence service.', revoked: 'The service says this key is revoked.',
             unreachable: 'The licence service could not be reached; the local key stands.',
             unknown: 'The service does not know this key.', current: 'Up to date.' }[s.refresh] || 'Refreshed.',
           s.refresh === 'revoked' || s.refresh === 'unknown');
    } catch (e) {
      note(e.message, true);
    }
  }

  async function remove() {
    const ok = await window.shellmateDialog.confirm({
      title: 'Remove the licence key?',
      body: 'ShellMate keeps working. Updating from inside the application stops until a key is installed again.',
      confirmLabel: 'Remove', danger: true,
    });
    if (!ok) return;
    try {
      const res = await fetch('/api/licence', { method: 'DELETE' });
      render(await res.json());
      note('Licence removed.');
    } catch (e) {
      note(e.message, true);
    }
  }

  function note(text, bad) {
    const el = document.getElementById('licence-note');
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('licence-note-bad', !!bad);
  }
})();
