/**
 * logs.js — Logs panel for ShellMate.
 * Shows available session log files, views them in place, downloads them.
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

    _initViewer();
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

      // What the folder is costing altogether (#243) — the sizes are already
      // in the listing, so the total is free.
      const totalBytes = files.reduce((sum, f) => sum + (f.size_bytes || 0), 0);
      const totalEl = document.createElement('div');
      totalEl.className = 'logs-total';
      totalEl.textContent = `${files.length} log${files.length === 1 ? '' : 's'} · `
        + (totalBytes >= 1048576
            ? `${(totalBytes / 1048576).toFixed(1)} MB`
            : `${(totalBytes / 1024).toFixed(1)} KB`)
        + ' in total';
      listEl.appendChild(totalEl);

      files.forEach(f => {
        // createElement throughout — the filename carries a device-supplied
        // hostname, and interpolating it into markup let a quote in a
        // hostname break the row.
        const row = document.createElement('div');
        row.className = 'log-row';

        const icon = document.createElement('span');
        icon.className = 'material-symbols-outlined log-icon';
        icon.textContent = 'description';

        const info = document.createElement('div');
        info.className = 'log-info';
        const name = document.createElement('span');
        name.className = 'log-name';
        name.textContent = f.filename;
        const meta = document.createElement('span');
        meta.className = 'log-meta';
        meta.textContent = `${new Date(f.modified).toLocaleString()} · `
          + `${(f.size_bytes / 1024).toFixed(1)} KB`;
        info.append(name, meta);

        // The row opens the viewer — reading a log should not require
        // leaving the application (#236).
        row.title = 'Click to view';
        row.addEventListener('click', () => openViewer(f));

        const dl = document.createElement('button');
        dl.type = 'button';
        dl.className = 'log-download btn-secondary';
        dl.title = 'Download';
        const dlIcon = document.createElement('span');
        dlIcon.className = 'material-symbols-outlined';
        dlIcon.textContent = 'download';
        dl.appendChild(dlIcon);
        dl.addEventListener('click', (e) => {
          e.stopPropagation();
          downloadLog(f.filename);
        });

        row.append(icon, info, dl);
        listEl.appendChild(row);
      });
    } catch (e) {
      listEl.innerHTML = '<div class="logs-empty logs-error">Failed to load logs.</div>';
    }
  }

  // -------------------------------------------------------------------------
  // Downloading (#234)
  // -------------------------------------------------------------------------

  /**
   * Fetch the file and hand it to the browser as a blob.
   *
   * The old form was a bare `<a download>`, which leans entirely on the
   * browser's download machinery — and in the native window that machinery
   * quietly did nothing, with no error to see. Fetching first means a failure
   * has a status code and a toast, and success is announced rather than
   * assumed.
   */
  async function downloadLog(filename) {
    // In the native window, save through the platform's folder dialog and
    // open the folder (#578): a browser download there did nothing anyone
    // could see. Without a native window the server answers 409 and the
    // browser download below is the right shape.
    try {
      const saved = await fetch(`/api/logs/${encodeURIComponent(filename)}/save`, { method: 'POST' });
      if (saved.ok) {
        const data = await saved.json();
        if (data.cancelled) return;
        if (window.shellmateAlerts && window.shellmateAlerts.notify) {
          window.shellmateAlerts.notify({
            global: true, icon: 'download', title: 'Log saved',
            body: `Saved to ${data.path}. The folder has been opened.`,
          });
        }
        return;
      }
      if (saved.status !== 409) {
        const err = await saved.json().catch(() => ({}));
        throw new Error(err.detail || `server returned ${saved.status}`);
      }
    } catch (e) {
      if (window.shellmateAlerts && window.shellmateAlerts.notify) {
        window.shellmateAlerts.notify({ global: true, severity: 'warning', icon: 'error',
                                        title: 'Could not save the log', body: String(e.message || e) });
      }
      return;
    }
    try {
      const res = await fetch(`/api/logs/${encodeURIComponent(filename)}`);
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      if (window.shellmateAlerts && window.shellmateAlerts.notify) {
        window.shellmateAlerts.notify({
          global: true,
          icon:  'download',
          title: 'Download started',
          body:  `${filename} — check your browser's downloads folder.`,
          // Somewhere to click (#245). The browser's downloads folder is
          // not ours to open; the originals' folder is.
          action: {
            label: 'Open the log folder',
            onClick: () => fetch('/api/logs/reveal', { method: 'POST' })
              .catch(() => { /* the folder not opening is not worth an error */ }),
          },
        });
      }
    } catch (e) {
      if (window.shellmateAlerts && window.shellmateAlerts.notify) {
        window.shellmateAlerts.notify({
          global: true,
          severity: 'warning',
          icon:  'error',
          title: 'Download failed',
          body:  String(e.message || e),
        });
      }
    }
  }

  // -------------------------------------------------------------------------
  // The viewer (#236)
  // -------------------------------------------------------------------------

  /** Longest text the viewer will render; beyond it, the tail wins. */
  const VIEW_LIMIT = 2_000_000;

  let _viewerText = '';
  let _viewerFile = '';

  function _initViewer() {
    const overlayEl = document.getElementById('logview-overlay');
    if (!overlayEl) return;

    document.getElementById('logview-close')
      .addEventListener('click', closeViewer);
    overlayEl.addEventListener('click', (e) => {
      if (e.target === overlayEl) closeViewer();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlayEl.classList.contains('hidden')) {
        closeViewer();
      }
    });

    document.getElementById('logview-copy')
      .addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(_viewerText);
        } catch (_) {
          // The fallback route older webviews need.
          const ta = document.createElement('textarea');
          ta.value = _viewerText;
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand('copy'); } catch (_) { /* give up */ }
          ta.remove();
        }
        if (typeof window._showCopyToast === 'function') window._showCopyToast();
      });

    document.getElementById('logview-download')
      .addEventListener('click', () => {
        if (_viewerFile) downloadLog(_viewerFile);
      });
  }

  async function openViewer(f) {
    const overlayEl = document.getElementById('logview-overlay');
    const content = document.getElementById('logview-content');
    const metaEl = document.getElementById('logview-meta');
    if (!overlayEl || !content) return;

    document.getElementById('logview-title').textContent = f.filename;
    metaEl.textContent = `${new Date(f.modified).toLocaleString()} · `
      + `${(f.size_bytes / 1024).toFixed(1)} KB`;
    content.textContent = 'Loading…';
    overlayEl.classList.remove('hidden');

    try {
      const res = await fetch(`/api/logs/${encodeURIComponent(f.filename)}`);
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const text = await res.text();
      _viewerText = text;
      _viewerFile = f.filename;
      // The tail, not the head: on a log too big to show whole, the recent
      // end is the part someone opens it for. Copy still takes everything.
      content.textContent = text.length > VIEW_LIMIT
        ? `[…first ${(text.length - VIEW_LIMIT).toLocaleString()} characters `
          + `not shown — download or copy for the whole file…]\n`
          + text.slice(-VIEW_LIMIT)
        : text;
      content.scrollTop = content.scrollHeight;
    } catch (e) {
      _viewerText = '';
      _viewerFile = '';
      content.textContent = `Could not load the log: ${e.message || e}`;
    }
  }

  function closeViewer() {
    const overlayEl = document.getElementById('logview-overlay');
    if (overlayEl) overlayEl.classList.add('hidden');
    _viewerText = '';
    _viewerFile = '';
  }

  window.openLogs = openLogs;
})();
