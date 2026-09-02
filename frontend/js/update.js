/**
 * update.js — Updates, from the user's side (#420, #441).
 *
 * Three things:
 *
 *   - **Check for updates** from the sidebar (and Diagnostics, and the tray):
 *     one request to GitHub's releases API, the answer as a toast with a
 *     link. Nothing is downloaded.
 *   - **The startup check**, only when `diag.update_check` is on in Stockton,
 *     because ShellMate is meant to work with no internet at all.
 *   - **What's new on the first run of a new version**: the last version
 *     announced is kept in settings; when the running build is newer, one
 *     toast offers the manual's What's new page. Never on a fresh install,
 *     where there is nothing to have upgraded from.
 */
(function () {
  'use strict';

  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

  document.addEventListener('DOMContentLoaded', () => {
    const link = document.getElementById('sidebar-link-updates');
    if (link) link.addEventListener('click', (e) => { e.preventDefault(); checkNow(); });
    // Settings arrive over the same wire; give them a moment.
    setTimeout(announceIfNew, 4000);
    setTimeout(startupCheck, 8000);
  });

  function toast(spec) {
    if (window.shellmateAlerts) window.shellmateAlerts.notify(spec);
  }

  async function fetchStatus() {
    const res = await fetch('/api/system/update');
    return res.json();
  }

  async function checkNow() {
    toast({ severity: 'info', icon: 'download', title: 'Checking for updates', body: 'Asking GitHub…' });
    let info;
    try {
      info = await fetchStatus();
    } catch (e) {
      toast({ severity: 'warning', icon: 'error', title: 'Could not check', body: e.message });
      return;
    }
    if (info.error) {
      toast({ severity: 'warning', icon: 'error', title: 'Could not check for updates', body: info.error });
    } else if (info.note) {
      toast({ severity: 'info', icon: 'download', title: 'No release yet',
              body: `${info.note} You are running ${info.current}.` });
    } else if (info.newer) {
      toast({ severity: 'info', icon: 'download',
              title: `ShellMate ${info.latest} is available`,
              body: `You are running ${info.current}.`,
              action: { label: 'Open the release page', onClick: () => window.open(info.url, '_blank', 'noopener') } });
    } else {
      toast({ severity: 'info', icon: 'download', title: 'You are up to date',
              body: `ShellMate ${info.current} is the latest release.` });
    }
  }

  async function startupCheck() {
    if (!A('diag.update_check', false)) return;
    let info;
    try { info = await fetchStatus(); } catch (_) { return; }   // air-gapped: say nothing
    if (!info || !info.newer) return;
    toast({ severity: 'info', icon: 'download',
            title: `ShellMate ${info.latest} is available`,
            body: `You are running ${info.current}.`,
            action: { label: 'Open the release page', onClick: () => window.open(info.url, '_blank', 'noopener') } });
  }

  /** "1.2.3" → [1,2,3]; anything unparseable → []. */
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
    try {
      info = await (await fetch('/api/system/info')).json();
    } catch (_) { return; }
    const current = info && info.version;
    if (!current) return;
    const ui = (window.shellmateSettings || {}).interface || {};
    const seen = ui.last_seen_version;
    if (seen === undefined) return;                 // settings not loaded yet
    if (seen === '') { remember(current); return; } // a fresh install: nothing to announce
    if (!newer(current, seen)) return;
    toast({ severity: 'info', icon: 'bolt',
            title: `ShellMate ${current} — see what's new`,
            body: `This copy is newer than the ${seen} you had. The manual lists what changed.`,
            action: { label: "What's new", onClick: () => {
              if (typeof window.openDocsPage === 'function') window.openDocsPage('whats-new.md');
            } } });
    remember(current);
  }

  function remember(version) {
    window.shellmateSettings = window.shellmateSettings || {};
    window.shellmateSettings.interface = Object.assign({}, window.shellmateSettings.interface, { last_seen_version: version });
    fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { interface: { last_seen_version: version } } }),
    }).catch(() => {});
  }

  window.checkForUpdates = checkNow;
})();
