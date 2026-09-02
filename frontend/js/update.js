/**
 * update.js — The startup release check (#420), when it is switched on.
 *
 * Off unless `diag.update_check` is on in Stockton. When it is, one request
 * goes to GitHub a few seconds after launch and, if a newer release exists,
 * one notification says so with the version numbers. Nothing is downloaded
 * and nothing repeats: it is a hint, not a nag.
 */
(function () {
  'use strict';

  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

  document.addEventListener('DOMContentLoaded', () => {
    // Settings arrive over the same wire; give them a moment.
    setTimeout(check, 8000);
  });

  async function check() {
    if (!A('diag.update_check', false)) return;
    let info;
    try {
      info = await (await fetch('/api/system/update')).json();
    } catch (_) {
      return;                       // air-gapped, most likely — say nothing
    }
    if (!info || !info.newer || !window.shellmateAlerts) return;
    window.shellmateAlerts.notify({
      severity: 'info', icon: 'download',
      title: `ShellMate ${info.latest} is available`,
      body: `You are running ${info.current}. The download is under Settings → Diagnostics → Check for updates.`,
    });
  }
})();
