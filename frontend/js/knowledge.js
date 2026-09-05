/**
 * knowledge.js — The folder the assistant reads your own documents from (#561).
 *
 * `chroma_client.js`'s promise, kept without a server. Everything here is
 * about one question: is there anything indexed, and if not, what does the
 * engineer have to do next.
 *
 * **The path is shown even when the folder does not exist.** "Put your
 * documents here" needs somewhere to point at, and a panel that stayed blank
 * until somebody had already made the folder would never tell them to.
 *
 * **Nothing indexes on its own.** Reindexing walks a folder that may hold
 * fifty documents, and doing that silently on every page load would make a
 * settings panel feel broken on a slow disk. The button says what it will do.
 */
(function () {
  'use strict';

  function el(id) { return document.getElementById(id); }

  function init() {
    const refresh = el('knowledge-reindex');
    if (!refresh) return;

    refresh.addEventListener('click', () => reindex(false));
    el('knowledge-rebuild').addEventListener('click', () => reindex(true));
    el('knowledge-open').addEventListener('click', reveal);

    load();
  }

  async function load() {
    try {
      const res = await fetch('/api/knowledge');
      paint(await res.json());
    } catch (_) { /* the panel simply stays as it is */ }
  }

  function paint(data) {
    const path = el('knowledge-folder');
    if (path) path.textContent = data.folder || '';

    const status = el('knowledge-status');
    if (!status) return;

    if (!data.exists) {
      status.textContent = 'That folder does not exist yet. Open it to have '
        + 'ShellMate make it, then drop .md or .txt files in and index them.';
      status.className = 'settings-section-hint';
      return;
    }
    if (!data.files) {
      status.textContent = 'Nothing indexed yet. Put .md or .txt files in the '
        + 'folder and press Index new and changed files.';
      status.className = 'settings-section-hint';
      return;
    }

    // The search mode is named because "why are my results poor" has two
    // very different answers depending on whether this build has FTS5, and
    // that should be visible somewhere other than the log.
    const mode = data.search === 'fts5'
      ? '' : ' Full-text search is unavailable in this build, so matching '
           + 'falls back to a plain scan — expect poorer results on long '
           + 'documents.';
    status.textContent = `${data.files} document`
      + `${data.files === 1 ? '' : 's'}, ${data.chunks} passage`
      + `${data.chunks === 1 ? '' : 's'} indexed`
      + `${data.indexed_at ? `, last on ${when(data.indexed_at)}` : ''}.${mode}`;
    status.className = data.search === 'fts5'
      ? 'settings-section-hint' : 'settings-section-hint knowledge-degraded';
  }

  function when(stamp) {
    const date = new Date(Number(stamp) * 1000);
    return Number.isNaN(date.getTime()) ? String(stamp) : date.toLocaleString();
  }

  async function reindex(force) {
    const buttons = [el('knowledge-reindex'), el('knowledge-rebuild')];
    buttons.forEach(b => { if (b) b.disabled = true; });
    const status = el('knowledge-status');
    if (status) {
      status.textContent = force ? 'Reading every file…' : 'Indexing…';
      status.className = 'settings-section-hint';
    }

    try {
      const res = await fetch('/api/knowledge/reindex', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: !!force }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (status) {
          status.textContent = data.detail || `HTTP ${res.status}`;
          status.className = 'settings-section-hint knowledge-degraded';
        }
        return;
      }
      await load();
      // What was passed over, and why. A document silently skipped for
      // being 4MB is a document somebody believes the assistant has read.
      renderSkipped(data.skipped || []);
    } catch (e) {
      if (status) {
        status.textContent = String(e.message || e);
        status.className = 'settings-section-hint knowledge-degraded';
      }
    } finally {
      buttons.forEach(b => { if (b) b.disabled = false; });
    }
  }

  function renderSkipped(skipped) {
    const host = el('knowledge-skipped');
    if (!host) return;
    host.innerHTML = '';
    host.classList.toggle('hidden', !skipped.length);
    if (!skipped.length) return;

    const heading = document.createElement('div');
    heading.className = 'settings-section-hint';
    heading.textContent = `${skipped.length} file`
      + `${skipped.length === 1 ? ' was' : 's were'} passed over:`;
    host.appendChild(heading);

    const list = document.createElement('ul');
    list.className = 'knowledge-skipped-list';
    skipped.forEach(item => {
      const row = document.createElement('li');
      // textContent: these are file names off the user's disk.
      row.textContent = `${item.file || '?'} — `
        + `${item.reason || 'skipped'}`;
      list.appendChild(row);
    });
    host.appendChild(list);
  }

  async function reveal() {
    try {
      const res = await fetch('/api/knowledge/reveal', { method: 'POST' });
      const data = await res.json();
      if (!data.opened) {
        const status = el('knowledge-status');
        if (status) {
          status.textContent = `ShellMate could not open a file manager. `
            + `The folder is ${data.folder}.`;
          status.className = 'settings-section-hint knowledge-degraded';
        }
      }
      // Made by the reveal, so the state is now different from what is on
      // the screen.
      load();
    } catch (_) { /* the path is on the screen either way */ }
  }

  document.addEventListener('DOMContentLoaded', init);
  window.shellmateKnowledge = { load };
})();
