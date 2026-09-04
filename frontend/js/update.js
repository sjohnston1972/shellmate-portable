/**
 * update.js — Updates, from the user's side (#420, #441, #442–#445, #448).
 *
 * The whole flow stays inside ShellMate:
 *
 *   1. A check runs shortly after start (on by default; the Stockton switch
 *      is for air-gapped sites) and from the sidebar, the tray and Diagnostics.
 *   2. A newer version opens ShellMate's own modal: version, date, size, the
 *      release notes, and Update now / Later / Skip this version. Snoozed and
 *      skipped versions are remembered.
 *   3. Update now downloads the executable into the data folder with a
 *      progress bar; the server verifies it against the release's checksum.
 *   4. Apply hands off to a helper that swaps the executable after this
 *      process exits and relaunches; the page waits for the new copy.
 *   5. The first run of a new version shows a styled what's-new modal.
 *
 * Downloading and applying need a licence (#448). Without one the modal
 * still shows what the new version contains and how to get a licence; the
 * release page link stays for anyone who prefers to fetch the file by hand.
 */
(function () {
  'use strict';

  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);
  const ui = () => (window.shellmateSettings || {}).interface || {};

  let overlay, box;
  let pollTimer = null;
  let current = null;                // the release being offered

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('update-overlay');
    box = document.getElementById('update-box');
    const link = document.getElementById('sidebar-link-updates');
    if (link) link.addEventListener('click', (e) => { e.preventDefault(); checkNow(); });
    if (overlay) overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && overlay && !overlay.classList.contains('hidden')) close();
    });
    setTimeout(announceIfNew, 4000);
    setTimeout(startupCheck, 8000);
    // What's new, on demand (#567): the modal used to show once and then
    // only the manual carried the notes.
    const again = document.getElementById('diag-whats-new');
    if (again) again.addEventListener('click', async () => {
      let info;
      try { info = await (await fetch('/api/system/info')).json(); } catch (_) { return; }
      if (info && info.version) openWhatsNew(info.version, '');
    });
  });

  function toast(spec) {
    if (window.shellmateAlerts) window.shellmateAlerts.notify({ global: true, ...spec });
  }

  async function fetchStatus() {
    const res = await fetch('/api/system/update');
    return res.json();
  }

  // ---------------------------------------------------------------- checks
  async function checkNow() {
    let info;
    try {
      info = await fetchStatus();
    } catch (e) {
      toast({ severity: 'warning', icon: 'error', title: 'Could not check for updates', body: e.message });
      return;
    }
    if (info.error) {
      toast({ severity: 'warning', icon: 'error', title: 'Could not check for updates', body: info.error });
    } else if (info.note) {
      toast({ severity: 'info', icon: 'download', title: 'No release yet', body: `${info.note} You are running ${info.current}.` });
    } else if (info.newer) {
      openModal(info);
    } else {
      toast({ severity: 'info', icon: 'download', title: 'You are up to date', body: `ShellMate ${info.current} is the latest release.` });
    }
  }

  async function startupCheck() {
    // A swap that the helper had to undo is said here, once (#450).
    try {
      const s = await (await fetch('/api/system/update/status')).json();
      const last = s && s.last_attempt;
      if (last && last.ok === false) {
        toast({ severity: 'warning', icon: 'error', title: 'The last update did not apply',
                body: `${last.detail || 'The helper put the previous version back.'} You are still on ${s.current || 'the previous version'}.`,
                sticky: true });
      }
    } catch (_) { /* nothing to report */ }
    if (!A('diag.update_check', true)) return;
    let info;
    try { info = await fetchStatus(); } catch (_) { return; }   // air-gapped: say nothing
    if (!info || !info.newer) return;
    const seen = ui();
    if (seen.skipped_version === info.latest) return;
    if (seen.snoozed_version === info.latest && Number(seen.snoozed_until) > Date.now()) return;
    openModal(info);
  }

  // ---------------------------------------------------------------- the modal
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function openModal(info) {
    if (!overlay || !box) return;
    current = info;
    box.innerHTML = '';
    const head = el('div', 'update-head');
    head.append(el('span', 'material-symbols-outlined update-icon', 'download'),
                el('h2', 'update-title', `ShellMate ${info.latest} is available`));
    if (info.prerelease) head.appendChild(el('span', 'update-badge', 'Beta'));   // #567
    box.appendChild(head);

    const meta = [];
    if (info.published) meta.push(`Published ${new Date(info.published).toLocaleDateString()}`);
    if (info.size) meta.push(`${(info.size / 1048576).toFixed(1)} MB`);
    meta.push(`You are running ${info.current}`);
    box.appendChild(el('div', 'update-meta', meta.join(' · ')));

    const notes = el('div', 'update-notes');
    if (window.shellmateMarkdown && info.notes) {
      notes.innerHTML = window.shellmateMarkdown.render(info.notes).html;
    } else {
      notes.textContent = info.notes || 'No release notes were published for this version.';
    }
    box.appendChild(notes);

    const licence = info.licence || {};
    const licensed = !!licence.valid;
    const status = el('div', 'update-licence ' + (licensed ? 'ok' : 'no'));
    status.textContent = licensed
      ? `Licensed: ${licence.detail || ''}`
      : (licence.detail || 'Updating from inside ShellMate needs a licence.');
    box.appendChild(status);
    if (!licensed) {
      const how = el('p', 'update-how');
      how.append(document.createTextNode('Paste a licence key under '), el('b', '', 'Settings → Licence'),
                 document.createTextNode(' to update here. Without one, the release can still be downloaded by hand from '));
      const a = el('a', '', 'the release page');
      a.href = info.url || '#'; a.target = '_blank'; a.rel = 'noopener';
      how.appendChild(a);
      how.appendChild(document.createTextNode(' and copied over the executable.'));
      box.appendChild(how);
    }

    const progress = el('div', 'update-progress hidden');
    const bar = el('div', 'update-bar');
    const fill = el('div', 'update-bar-fill');
    bar.appendChild(fill);
    const label = el('div', 'update-progress-label', '');
    progress.append(bar, label);
    box.appendChild(progress);

    const actions = el('div', 'update-actions');
    const primary = el('button', 'btn-primary', licensed ? 'Update now' : 'Open Settings → Licence');
    primary.type = 'button';
    const later = el('button', 'btn-secondary', 'Later');
    later.type = 'button';
    const skip = el('button', 'btn-tertiary', 'Skip this version');
    skip.type = 'button';
    const page = el('a', 'update-page-link', 'Release page');
    page.href = info.url || '#'; page.target = '_blank'; page.rel = 'noopener';
    actions.append(primary, later, skip, page);
    box.appendChild(actions);

    primary.addEventListener('click', () => {
      if (!licensed) {
        close();
        if (typeof window.openSettingsSection === 'function') window.openSettingsSection('Licence');
        else if (typeof window.openSettings === 'function') window.openSettings();
        return;
      }
      startDownload(progress, fill, label, primary, later, skip);
    });

    // Already downloaded and verified — from an earlier visit, or a
    // download that finished while the modal was closed — goes straight
    // to the restart, rather than fetching the file again (#450).
    if (licensed) {
      fetch('/api/system/update/status').then(r => r.json()).then((s) => {
        if (!s || s.phase !== 'ready' || s.version !== info.latest) return;
        progress.classList.remove('hidden');
        fill.style.width = '100%';
        label.textContent = `ShellMate ${s.version} is downloaded and verified.`;
        primary.textContent = 'Restart into the new version';
        primary.onclick = (e) => { e.stopImmediatePropagation(); applyNow(label, primary, later); };
      }).catch(() => {});
    }
    later.addEventListener('click', () => { remember({ snoozed_version: info.latest, snoozed_until: Date.now() + 24 * 3600 * 1000 }); close(); });
    skip.addEventListener('click', () => { remember({ skipped_version: info.latest }); close(); });

    overlay.classList.remove('hidden');
    primary.focus();
  }

  function close() {
    if (overlay) overlay.classList.add('hidden');
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---------------------------------------------------------------- download and apply
  async function startDownload(progress, fill, label, primary, later, skip) {
    primary.disabled = true; later.disabled = true; skip.disabled = true;
    progress.classList.remove('hidden');
    label.textContent = 'Starting the download…';
    try {
      const res = await fetch('/api/system/update/download', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    } catch (e) {
      label.textContent = e.message;
      primary.disabled = false; later.disabled = false; skip.disabled = false;
      return;
    }
    pollTimer = setInterval(async () => {
      let s;
      try { s = await (await fetch('/api/system/update/status')).json(); } catch (_) { return; }
      if (s.phase === 'downloading') {
        const pct = s.total ? Math.round(100 * s.received / s.total) : 0;
        fill.style.width = `${pct}%`;
        label.textContent = `Downloading ${s.version}… ${(s.received / 1048576).toFixed(1)} of ${(s.total / 1048576).toFixed(1)} MB`;
      } else if (s.phase === 'verifying') {
        fill.style.width = '100%';
        label.textContent = 'Verifying the download against the release checksum…';
      } else if (s.phase === 'ready') {
        clearInterval(pollTimer); pollTimer = null;
        fill.style.width = '100%';
        label.textContent = 'Downloaded and verified.';
        primary.textContent = 'Restart into the new version';
        primary.disabled = false; later.disabled = false;
        primary.onclick = () => applyNow(label, primary, later);
      } else if (s.phase === 'failed') {
        clearInterval(pollTimer); pollTimer = null;
        label.textContent = s.error || 'The download failed.';
        primary.disabled = false; later.disabled = false; skip.disabled = false;
      }
    }, 500);
  }

  async function applyNow(label, primary, later) {
    const live = typeof window.getOpenTabs === 'function'
      ? window.getOpenTabs().filter(t => t.isConnected).length : 0;
    const ok = await window.shellmateDialog.confirm({
      title: 'Restart into the new version?',
      body: (live ? `${live} connected session${live === 1 ? '' : 's'} will be closed. ` : '')
          + 'ShellMate closes, the executable is replaced, and the new copy starts. '
          + 'If it does not come up, the previous one is put back.',
      confirmLabel: 'Restart and update',
    });
    if (!ok) return;
    primary.disabled = true; later.disabled = true;
    label.textContent = 'Handing over to the update helper…';
    try {
      const res = await fetch('/api/system/update/apply', { method: 'POST' });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Server error ${res.status}`);
      }
    } catch (e) {
      // A refusal comes back; a success does not answer at all — the
      // process is gone — and lands in the catch as a network error.
      if (!/fetch|network|Failed/i.test(e.message)) {
        label.textContent = e.message;
        primary.disabled = false; later.disabled = false;
        return;
      }
    }
    waitForRelaunch(label);
  }

  function waitForRelaunch(label) {
    label.textContent = 'ShellMate is restarting. This page reconnects when the new copy answers…';
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      try {
        const res = await fetch('/api/health', { cache: 'no-store' });
        if (res.ok) { clearInterval(timer); location.reload(); }
      } catch (_) { /* not yet */ }
      if (tries > 120) { clearInterval(timer); label.textContent = 'The new copy has not answered. Check ShellMate-Data/updates and start ShellMate by hand.'; }
    }, 1000);
  }

  // ---------------------------------------------------------------- what's new
  function parse(v) {
    return String(v || '').replace(/^v/i, '').split('.').map(x => parseInt(x, 10)).filter(n => !Number.isNaN(n));
  }
  function newer(a, b) {
    const x = parse(a), y = parse(b);
    if (!x.length || !y.length) return false;
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      const d = (x[i] || 0) - (y[i] || 0);
      if (d) return d > 0;
    }
    return false;
  }

  async function announceIfNew() {
    let info;
    try { info = await (await fetch('/api/system/info')).json(); } catch (_) { return; }
    const version = info && info.version;
    if (!version) return;
    const seen = ui().last_seen_version;
    if (seen === undefined) return;                 // settings not loaded yet
    if (seen === '') { remember({ last_seen_version: version }); return; }   // a fresh install
    if (!newer(version, seen)) return;
    openWhatsNew(version, seen);
    remember({ last_seen_version: version });
  }

  async function openWhatsNew(version, seen) {
    if (!overlay || !box) return;
    let notes = '';
    try {
      const page = await (await fetch('/static/docs/whats-new.md')).text();
      const m = page.match(new RegExp(`^## ${version.replace(/\\./g, '\\\\.')}\\s*$([\\s\\S]*?)(?=^## |(?![\\s\\S]))`, 'm'));
      notes = m ? m[1].trim() : '';
    } catch (_) { /* the page still opens */ }
    box.innerHTML = '';
    const head = el('div', 'update-head');
    head.append(el('span', 'material-symbols-outlined update-icon', 'bolt'),
                el('h2', 'update-title', `Welcome to ShellMate ${version}`));
    box.appendChild(head);
    box.appendChild(el('div', 'update-meta', seen ? `Updated from ${seen}. Here is what changed.` : 'What changed in this version.'));
    const body = el('div', 'update-notes');
    if (window.shellmateMarkdown && notes) body.innerHTML = window.shellmateMarkdown.render(notes).html;
    else body.textContent = 'The manual\'s What\'s new page lists what changed.';
    box.appendChild(body);
    const actions = el('div', 'update-actions');
    const manual = el('button', 'btn-primary', 'Read the manual');
    manual.type = 'button';
    manual.addEventListener('click', () => { close(); if (typeof window.openDocsPage === 'function') window.openDocsPage('whats-new.md'); });
    const done = el('button', 'btn-secondary', 'Close');
    done.type = 'button';
    done.addEventListener('click', close);
    actions.append(manual, done);
    box.appendChild(actions);
    overlay.classList.remove('hidden');
    manual.focus();
  }

  function remember(changes) {
    window.shellmateSettings = window.shellmateSettings || {};
    window.shellmateSettings.interface = Object.assign({}, window.shellmateSettings.interface, changes);
    fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { interface: changes } }),
    }).catch(() => {});
  }

  window.checkForUpdates = checkNow;
  window.openUpdateModal = openModal;
  window.openWhatsNew = (version) => openWhatsNew(version, '');
})();
