/**
 * logs.js — Logs panel for ShellMate.
 * Shows available session log files, views them in place, downloads them.
 *
 * Search and a date range (#576). The panel listed files by name and opened
 * one at a time, which works while you remember which session it was, and
 * stops working at exactly the point somebody needs it — "we changed
 * something on a Tuesday and I do not remember which switch."
 *
 * Two filters, because people arrive with either question. The dates narrow
 * the list; the query searches inside the files and reports which line. A
 * search that only matched filenames would send somebody back to reading
 * one log at a time, which is where they started.
 *
 * The server bounds what it reads and says when it stopped early, and the
 * panel repeats that on screen rather than swallowing it: an unannounced
 * bound makes "no matches" and "I did not look" the same answer.
 */
(function () {
  'use strict';

  let overlay;

  /** What is being searched for, and how. */
  const search = { q: '', case: false, word: false, regex: false,
                   from: '', to: '' };
  let searchTimer = null;

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('logs-overlay');

    document.getElementById('sidebar-link-logs')
      .addEventListener('click', (e) => { e.preventDefault(); openLogs(); });

    document.getElementById('logs-close')
      .addEventListener('click', closeLogs);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeLogs();
    });

    _initToolbar();
    _initViewer();
  });

  /**
   * The search box, the three switches and the date range.
   *
   * Typing is debounced because every keystroke is a pass over every log
   * file on the server side; the switches and the dates are not, because
   * those are one deliberate click and waiting after one feels broken.
   */
  function _initToolbar() {
    const query = document.getElementById('logs-query');
    if (!query) return;

    query.addEventListener('input', () => {
      search.q = query.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(refreshLogsList, 250);
    });
    // Enter searches now rather than waiting out the debounce, which is
    // what somebody who has finished typing expects it to do.
    query.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { clearTimeout(searchTimer); refreshLogsList(); }
    });

    [['logs-opt-case', 'case'], ['logs-opt-word', 'word'],
     ['logs-opt-regex', 'regex']].forEach(([id, key]) => {
      const btn = document.getElementById(id);
      if (!btn) return;
      btn.addEventListener('click', () => {
        search[key] = !search[key];
        btn.setAttribute('aria-pressed', String(search[key]));
        refreshLogsList();
      });
    });

    ['logs-from', 'logs-to'].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('change', () => {
        search.from = document.getElementById('logs-from').value;
        search.to = document.getElementById('logs-to').value;
        refreshLogsList();
      });
    });

    const clear = document.getElementById('logs-dates-clear');
    if (clear) clear.addEventListener('click', () => {
      document.getElementById('logs-from').value = '';
      document.getElementById('logs-to').value = '';
      search.from = '';
      search.to = '';
      refreshLogsList();
    });
  }

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

  /** A file row: icon, name, date and size, and the download button. */
  function _fileRow(f, onOpen) {
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

    row.title = 'Click to view';
    row.addEventListener('click', onOpen);

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
    return row;
  }

  /**
   * Show the files a search matched, with the lines it matched on.
   *
   * The hit count is on the row and the first few lines are under it: the
   * count says which file to look in, the lines say whether it is the right
   * one without opening it.
   */
  function _renderSearch(listEl, data) {
    listEl.innerHTML = '';

    if (!data.files.length) {
      const empty = document.createElement('div');
      empty.className = 'logs-empty';
      empty.textContent = data.searched
        ? `Nothing matched in ${data.searched} log`
          + `${data.searched === 1 ? '' : 's'}.`
        : 'No logs fall in that date range.';
      listEl.appendChild(empty);
      return;
    }

    const total = document.createElement('div');
    total.className = 'logs-total';
    // With dates alone there is nothing to count matches of, and a hit
    // total of zero above a list of files reads as a failed search.
    total.textContent = search.q.trim()
      ? `${data.hits} match${data.hits === 1 ? '' : 'es'} in `
        + `${data.files.length} of ${data.searched} log`
        + `${data.searched === 1 ? '' : 's'}`
      : `${data.files.length} log${data.files.length === 1 ? '' : 's'} `
        + 'in that date range';
    listEl.appendChild(total);

    // Said, not implied. A truncated search that stayed quiet about it
    // makes "no matches" and "I did not look" indistinguishable.
    if (data.truncated) {
      const warn = document.createElement('div');
      warn.className = 'logs-bounded';
      warn.textContent = 'Some logs were too long to search all the way '
        + 'through, so there may be matches further back in them. The limit '
        + 'is under Settings → Advanced → Session log search.';
      listEl.appendChild(warn);
    }

    data.files.forEach((f) => {
      const row = _fileRow(f, () => openViewer(f, { line: 1 }));
      const hits = document.createElement('span');
      hits.className = 'log-hits';
      hits.textContent = f.hits === 1 ? '1 match' : `${f.hits} matches`;
      if (f.capped) {
        hits.title = `Showing the first ${f.matches.length} of ${f.hits}.`;
      }
      // Before the download button, so the count reads with the name.
      row.insertBefore(hits, row.lastChild);
      listEl.appendChild(row);

      const matches = document.createElement('div');
      matches.className = 'log-matches';
      f.matches.forEach((m) => {
        const hit = document.createElement('div');
        hit.className = 'log-match';
        hit.title = 'Open the log here';

        const num = document.createElement('span');
        num.className = 'log-match-line';
        num.textContent = String(m.line);

        const text = document.createElement('span');
        text.className = 'log-match-text';
        _mark(text, m.text);

        hit.append(num, text);
        hit.addEventListener('click', () => openViewer(f, { line: m.line }));
        matches.appendChild(hit);
      });
      listEl.appendChild(matches);
    });
  }

  async function refreshLogsList() {
    const listEl = document.getElementById('logs-list');
    listEl.innerHTML = '<div class="logs-loading">Loading...</div>';

    // A query, a date range, or both. With a query the server searches
    // inside the files; with only dates it still goes through the search
    // endpoint, because filtering the listing here and filtering it there
    // are two implementations of one rule, and they would drift.
    if (search.q.trim() || search.from || search.to) {
      const params = new URLSearchParams({
        q: search.q.trim(), since: search.from, until: search.to,
        regex: String(search.regex), case: String(search.case),
        whole_word: String(search.word),
      });
      try {
        const res = await fetch(`/api/logs/search?${params}`);
        const data = await res.json();
        if (!res.ok) {
          listEl.innerHTML = '';
          const bad = document.createElement('div');
          bad.className = 'logs-empty logs-error';
          bad.textContent = data.detail || `Search failed (${res.status}).`;
          listEl.appendChild(bad);
          return;
        }
        _renderSearch(listEl, data);
      } catch (e) {
        listEl.innerHTML = '<div class="logs-empty logs-error">'
          + 'The search could not be run.</div>';
      }
      return;
    }

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

      files.forEach((f) => {
        listEl.appendChild(_fileRow(f, () => openViewer(f)));
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

  /** How many lines either side of a jumped-to match the viewer renders. */
  const JUMP_CONTEXT = 400;

  /** A newline. Named because it appears inside template literals
   *  where an escape is easy to lose in an edit. */
  const chr10 = String.fromCharCode(10);

  /**
   * The current query as a RegExp, for highlighting — or null.
   *
   * Built from the same three switches the server was given, so what is
   * marked in the viewer is what was searched for. A query that will not
   * compile returns null rather than throwing: the server has already
   * refused it and said so, and the viewer failing as well would be the
   * same complaint twice.
   */
  function _pattern() {
    const raw = search.q.trim();
    if (!raw) return null;
    let body = search.regex ? raw : raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (search.word) body = `\\b(?:${body})\\b`;
    try {
      return new RegExp(body, search.case ? 'g' : 'gi');
    } catch (_) {
      return null;
    }
  }

  /**
   * Write text into an element with the matches wrapped in <mark>.
   *
   * Built out of text nodes rather than innerHTML: this is device output,
   * and a log line containing a tag is the ordinary case, not the attack.
   */
  function _mark(el, text) {
    const pattern = _pattern();
    if (!pattern) { el.textContent = text; return; }

    let last = 0;
    let match;
    pattern.lastIndex = 0;
    while ((match = pattern.exec(text)) !== null) {
      // A pattern that can match nothing — `x*`, or an empty alternation —
      // would otherwise spin here forever with the browser unresponsive.
      if (match.index === pattern.lastIndex) { pattern.lastIndex += 1; continue; }
      if (match.index > last) {
        el.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      const hit = document.createElement('mark');
      hit.textContent = match[0];
      el.appendChild(hit);
      last = match.index + match[0].length;
    }
    if (last < text.length) {
      el.appendChild(document.createTextNode(text.slice(last)));
    }
  }

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
      if (jump && jump.line) {
        // Opened from a search hit: show where the match is, not the tail.
        _renderWindow(content, text, jump.line);
      } else {
        // The tail, not the head: on a log too big to show whole, the
        // recent end is the part someone opens it for. Copy takes all.
        content.textContent = text.length > VIEW_LIMIT
          ? `[…first ${(text.length - VIEW_LIMIT).toLocaleString()} characters `
            + `not shown — download or copy for the whole file…]` + chr10
            + text.slice(-VIEW_LIMIT)
          : text;
        content.scrollTop = content.scrollHeight;
      }
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

  /**
   * Open one log file by name (#534).
   *
   * The tab menu and the status-bar chip both know which file a session is
   * writing to, and "show me" is the next thing anybody wants. The entry is
   * looked up so the viewer has its size and date; a file that does not
   * exist yet — a log with nothing written to it — falls back to the list
   * rather than to an error, because "there is nothing in it yet" is the
   * honest answer and the list says so.
   */
  window.viewLogFile = async (filename) => {
    if (filename) {
      try {
        const res = await fetch('/api/logs');
        const files = res.ok ? await res.json() : [];
        const found = files.find(f => f.filename === filename);
        if (found) { await openViewer(found); return true; }
      } catch (_) { /* the list below is the fallback */ }
    }
    await openLogs();
    return false;
  };

  window.openLogs = openLogs;
})();
