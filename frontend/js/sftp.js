/**
 * sftp.js — Remote file browser for the active SSH tab.
 *
 * Browses, downloads, uploads and deletes files over the SFTP channel the
 * backend multiplexes onto the SSH connection the tab already has open. No
 * second login and no separate tool: pulling a config off a switch happens in
 * the same place you are already logged in.
 *
 * Only meaningful for SSH sessions. Serial and telnet have no file transfer,
 * and plenty of network devices run an SSH shell with no SFTP subsystem at
 * all, so the panel has to explain that clearly rather than showing an empty
 * list that looks like an empty directory.
 */
(function () {
  'use strict';

  let overlay, listEl, pathInput, statusEl, uploadInput;
  let currentPath = '.';

  document.addEventListener('DOMContentLoaded', () => {
    overlay     = document.getElementById('sftp-overlay');
    listEl      = document.getElementById('sftp-list');
    pathInput   = document.getElementById('sftp-path');
    statusEl    = document.getElementById('sftp-status');
    uploadInput = document.getElementById('sftp-upload-input');

    document.getElementById('sidebar-link-sftp')
      .addEventListener('click', (e) => { e.preventDefault(); openSftp(); });

    document.getElementById('sftp-close').addEventListener('click', closeSftp);
    document.getElementById('sftp-up').addEventListener('click', goUp);
    document.getElementById('sftp-go').addEventListener('click', () => browse(pathInput.value));

    document.getElementById('sftp-upload-btn')
      .addEventListener('click', () => uploadInput.click());

    uploadInput.addEventListener('change', handleUpload);

    pathInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); browse(pathInput.value); }
    });

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeSftp();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) closeSftp();
    });
  });

  // -------------------------------------------------------------------------
  // Panel visibility
  // -------------------------------------------------------------------------

  function activeTab() {
    return typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
  }

  function activeSessionId() {
    const tab = activeTab();
    return tab ? tab.sessionId : null;
  }

  async function openSftp() {
    overlay.classList.remove('hidden');
    currentPath = '.';
    await browse('.');
  }

  function closeSftp() {
    overlay.classList.add('hidden');
  }

  function goUp() {
    // The backend returns the resolved parent with each listing, so walking up
    // never has to guess at path separators.
    if (listEl.dataset.parent) browse(listEl.dataset.parent);
  }

  // -------------------------------------------------------------------------
  // Browsing
  // -------------------------------------------------------------------------

  async function browse(path) {
    const tab = activeTab();
    if (!tab) {
      showMessage('Open an SSH session first — file transfer runs over that connection.');
      return;
    }
    // Catch the wrong transport here rather than making the user wait for a
    // round trip to be told the same thing.
    if (tab.connectionType && tab.connectionType !== 'ssh') {
      showMessage(
        `This tab is ${tab.connectionType}, which has no file transfer. ` +
        `Switch to an SSH tab to browse files.`
      );
      return;
    }
    const sessionId = tab.sessionId;

    listEl.innerHTML = '<div class="sftp-loading">Loading…</div>';
    clearStatus();

    try {
      const res = await fetch(
        `/api/sftp/${sessionId}/list?path=${encodeURIComponent(path || '.')}`
      );
      const data = await res.json();

      if (!res.ok) {
        showMessage(data.detail || `Server error ${res.status}`);
        return;
      }

      currentPath = data.path;
      pathInput.value = data.path;
      listEl.dataset.parent = data.parent || '';
      render(data.entries);

    } catch (e) {
      showMessage('Could not reach the server.');
    }
  }

  function render(entries) {
    listEl.innerHTML = '';

    if (!entries.length) {
      listEl.innerHTML = '<div class="sftp-empty">This directory is empty.</div>';
      return;
    }

    entries.forEach(entry => {
      const row = document.createElement('div');
      row.className = 'sftp-row' + (entry.is_dir ? ' sftp-row-dir' : '');

      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined sftp-icon';
      icon.textContent = entry.is_dir ? 'list_alt' : 'description';

      const info = document.createElement('div');
      info.className = 'sftp-info';

      const name = document.createElement('span');
      name.className = 'sftp-name';
      // textContent, never innerHTML — a remote filename is untrusted input
      // and must not be parsed as markup.
      name.textContent = entry.name;

      const meta = document.createElement('span');
      meta.className = 'sftp-meta';
      meta.textContent = entry.is_dir
        ? entry.permissions
        : `${formatSize(entry.size)} · ${formatDate(entry.modified)} · ${entry.permissions}`;

      info.appendChild(name);
      info.appendChild(meta);

      row.appendChild(icon);
      row.appendChild(info);

      if (entry.is_dir) {
        row.addEventListener('click', () => browse(entry.path));
      } else {
        const download = document.createElement('button');
        download.className = 'sftp-action';
        download.title = `Download ${entry.name}`;
        download.innerHTML = '<span class="material-symbols-outlined">download</span>';
        download.addEventListener('click', (e) => {
          e.stopPropagation();
          const sessionId = activeSessionId();
          window.location.href =
            `/api/sftp/${sessionId}/download?path=${encodeURIComponent(entry.path)}`;
        });

        const remove = document.createElement('button');
        remove.className = 'sftp-action sftp-action-danger';
        remove.title = `Delete ${entry.name}`;
        remove.innerHTML = '<span class="material-symbols-outlined">delete_sweep</span>';
        remove.addEventListener('click', (e) => {
          e.stopPropagation();
          handleDelete(entry);
        });

        row.appendChild(download);
        row.appendChild(remove);
      }

      listEl.appendChild(row);
    });
  }

  // -------------------------------------------------------------------------
  // Transfers
  // -------------------------------------------------------------------------

  async function handleUpload(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';   // let the same file be picked again later
    if (!file) return;

    const sessionId = activeSessionId();
    if (!sessionId) { showMessage('Open an SSH session first.'); return; }

    const target = `${currentPath.replace(/\/$/, '')}/${file.name}`;
    showStatus(`Uploading ${file.name}…`);

    const body = new FormData();
    body.append('file', file);

    try {
      const res = await fetch(
        `/api/sftp/${sessionId}/upload?path=${encodeURIComponent(target)}`,
        { method: 'POST', body }
      );
      const data = await res.json();
      if (!res.ok) { showStatus(data.detail || `Upload failed (${res.status})`, true); return; }

      showStatus(`Uploaded ${file.name} (${formatSize(data.size)}).`);
      await browse(currentPath);
    } catch (err) {
      showStatus('Upload failed.', true);
    }
  }

  async function handleDelete(entry) {
    // Deleting a file off a live device is not undoable, so make the target
    // explicit rather than asking a generic "are you sure?".
    if (!window.confirm(`Delete ${entry.path} from the device?\n\nThis cannot be undone.`)) {
      return;
    }

    const sessionId = activeSessionId();
    try {
      const res = await fetch(
        `/api/sftp/${sessionId}/file?path=${encodeURIComponent(entry.path)}`,
        { method: 'DELETE' }
      );
      const data = await res.json();
      if (!res.ok) { showStatus(data.detail || `Delete failed (${res.status})`, true); return; }

      showStatus(`Deleted ${entry.name}.`);
      await browse(currentPath);
    } catch (err) {
      showStatus('Delete failed.', true);
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1048576).toFixed(1)} MB`;
    return `${(bytes / 1073741824).toFixed(2)} GB`;
  }

  function formatDate(epochSeconds) {
    if (!epochSeconds) return '';
    return new Date(epochSeconds * 1000).toLocaleString();
  }

  /** Replace the list with an explanatory message (wrong tab type, no SFTP). */
  function showMessage(text) {
    listEl.innerHTML = '';
    const box = document.createElement('div');
    box.className = 'sftp-empty';
    box.textContent = text;
    listEl.appendChild(box);
  }

  function showStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.classList.remove('hidden');
    statusEl.classList.toggle('sftp-status-error', Boolean(isError));
  }

  function clearStatus() {
    statusEl.textContent = '';
    statusEl.classList.add('hidden');
  }

  window.openSftp = openSftp;
})();
