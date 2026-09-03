/**
 * exit.js — Exit ShellMate from the status bar (#452).
 *
 * Closing the window hides it — sessions live in the server process, and a
 * device mid-reload must not lose its console because a window was shut.
 * Quitting was therefore only in the tray, which is easy to miss and does
 * not exist at all in the browser fallback. This is the explicit way out.
 *
 * It asks first when sessions are connected, saying how many, and otherwise
 * only if the "confirm before quitting" preference is on. Then it posts to
 * the server, which ends the tray, the window and itself together.
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('status-exit');
    if (button) button.addEventListener('click', exitShellMate);
  });

  async function exitShellMate() {
    const tabs = typeof window.getOpenTabs === 'function' ? window.getOpenTabs() : [];
    const live = tabs.filter(t => t.isConnected).length;
    const prefs = (window.shellmateSettings || {}).interface || {};
    const confirmAlways = prefs.confirm_quit !== false;
    if (live || confirmAlways) {
      const ok = await window.shellmateDialog.confirm({
        title: 'Exit ShellMate?',
        body: live
          ? `${live} connected session${live === 1 ? ' is' : 's are'} still open and will be closed. `
            + 'Anything a device is in the middle of — a reload, a pending commit — carries on without you watching.'
          : 'Every session closes and the application ends.',
        confirmLabel: 'Exit ShellMate',
        danger: Boolean(live),
      });
      if (!ok) return;
    }
    try {
      const res = await fetch('/api/system/quit', { method: 'POST' });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
    } catch (e) {
      // A refusal answers; a success may not, because the process is going.
      if (!/fetch|network|Failed/i.test(e.message)) {
        if (window.shellmateAlerts) {
          window.shellmateAlerts.notify({ global: true, severity: 'warning', icon: 'error',
                                          title: 'Could not exit', body: e.message });
        }
        return;
      }
    }
    farewell();
  }

  // The native window is destroyed by the server; a browser tab is not, so
  // it says what happened rather than showing a dead interface.
  function farewell() {
    const note = document.createElement('div');
    note.id = 'exit-farewell';
    note.innerHTML = '<div><span class="material-symbols-outlined">power</span>'
      + '<h2>ShellMate has closed</h2><p>Every session was ended. You can close this window.</p></div>';
    document.body.appendChild(note);
  }
})();
