/**
 * logs.js — Logs panel for ShellMate.
 * Shows available session log files and allows downloading them.
 */
(function () {
  'use strict';

  let overlay;

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('logs-overlay');

    document.getElementById('sidebar-link-logs')
      .addEventListener('click', (e) => { e.preventDefault(); openLogs(); });

    document.getElementById('logs-close')
      .addEventListener('click', closeLogs);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeLogs();
    });
  });

  async function openLogs() {
    overlay.classList.remove('hidden');
    await refreshLogsList();
  }

  function closeLogs() {
    overlay.classList.add('hidden');
  }


  /**
   * Turn file logging on or off without leaving this panel.
   *
   * Re-reads settings afterwards rather than patching the global, so
   * settings.js and this panel cannot end up disagreeing about the value —
   * the same reason the assistant toggle does it that way.
   */
  async function setLoggingEnabled(enabled) {
    try {
      await fetch('/api/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ settings: { logging: { enabled } } }),
      });
      if (typeof window.reloadSettings === 'function') await window.reloadSettings();
    } catch (e) {
      console.warn('Could not change the logging setting:', e);
    }
  }

  async function refreshLogsList() {
    const listEl = document.getElementById('logs-list');
    listEl.innerHTML = '<div class="logs-loading">Loading...</div>';
    try {
      const res = await fetch('/api/logs');
      const files = await res.json();
      if (files.length === 0) {
        // shellmateSettings, not shellshellmateSettings. The doubled "shell"
        // was never defined, so this was always {} and the panel claimed
        // logging was disabled whenever the list was empty — including when
        // it was on and there were simply no logs yet.
        const settings = window.shellmateSettings || {};
        const loggingEnabled = settings.logging && settings.logging.enabled;

        listEl.innerHTML = loggingEnabled
          ? '<div class="logs-empty">No log files yet. Start a session to generate logs.</div>'
          : '<div class="logs-empty">File logging is off. '
            + '<a href="#" id="logs-enable">Turn it on</a>, or '
            + '<a href="#" id="logs-goto-settings">open the setting</a>.</div>';

        // Turning it on from here rather than only pointing at the switch: it
        // is one setting, and somebody reading this has just demonstrated they
        // want it. Settings opens afterwards so the change is visible rather
        // than having happened somewhere they cannot see.
        const enable = document.getElementById('logs-enable');
        if (enable) {
          enable.addEventListener('click', async (e) => {
            e.preventDefault();
            await setLoggingEnabled(true);
            await refreshLogsList();
          });
        }

        const link = document.getElementById('logs-goto-settings');
        if (link) {
          link.addEventListener('click', (e) => {
            e.preventDefault();
            closeLogs();
            // Straight to the section, rather than wherever Settings was last
            // left. Telling somebody where to go and then not taking them
            // there is the half of this that was missing.
            if (typeof window.openSettingsSection === 'function') {
              window.openSettingsSection('Session Logging');
            } else if (typeof window.openSettings === 'function') {
              window.openSettings();
            }
          });
        }
        return;
      }
      listEl.innerHTML = '';
      files.forEach(f => {
        const row = document.createElement('div');
        row.className = 'log-row';
        const sizeKb = (f.size_bytes / 1024).toFixed(1);
        const date = new Date(f.modified).toLocaleString();
        row.innerHTML = `
          <span class="material-symbols-outlined log-icon">description</span>
          <div class="log-info">
            <span class="log-name">${f.filename}</span>
            <span class="log-meta">${date} &middot; ${sizeKb} KB</span>
          </div>
          <a class="log-download btn-secondary" href="/api/logs/${encodeURIComponent(f.filename)}" download="${f.filename}" title="Download">
            <span class="material-symbols-outlined">download</span>
          </a>
        `;
        listEl.appendChild(row);
      });
    } catch (e) {
      listEl.innerHTML = '<div class="logs-empty logs-error">Failed to load logs.</div>';
    }
  }

  window.openLogs = openLogs;
})();
