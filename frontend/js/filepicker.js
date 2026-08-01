/**
 * filepicker.js — Choosing a file on this machine.
 *
 * SSH key authentication needs a filesystem *path*. A browser's file input
 * deliberately withholds that and hands over the contents instead, so there
 * are only two ways to get one:
 *
 *   1. The platform's own dialog, which pywebview can raise when ShellMate is
 *      running in its native window. This is the real Explorer window and the
 *      better experience, so it is tried first.
 *   2. Walking the filesystem in the interface, backed by /api/local/browse.
 *      Slower, but it works when ShellMate has been opened in a browser —
 *      where option 1 does not exist at all.
 *
 * Both are here because either alone leaves the button dead in a situation
 * someone will actually be in.
 */
(function () {
  'use strict';

  let overlay, listEl, pathEl, upBtn, chooseBtn, titleEl;
  let currentPath = '';
  let selected = '';
  let onChoose = null;
  /** "file" or "folder" — a folder is chosen by standing in it. */
  let mode = 'file';

  document.addEventListener('DOMContentLoaded', () => {
    // Delegated: the Browse buttons live inside a modal that is built before
    // this script runs, and more may be added later.
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-browse');
      if (!btn) return;
      e.preventDefault();
      browseFor(btn.dataset.target, btn.dataset.pick || 'file');
    });

    build();
  });

  /**
   * Ask for a file or a folder for the given input, native dialog first.
   *
   * @param targetId  id of the input to fill in
   * @param wanted    "file" (an SSH key) or "folder" (where captures go)
   */
  async function browseFor(targetId, wanted) {
    const input = document.getElementById(targetId);
    if (!input) return;

    const folder = wanted === 'folder';

    try {
      const res = await fetch('/api/pick-file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: folder ? 'Choose a folder' : 'Select a private key file',
          // A folder field already holds a folder; a file field holds a file.
          directory: folder ? input.value : directoryOf(input.value),
          folder,
        }),
      });
      const data = await res.json();
      if (data.available) {
        // A native dialog was shown. An empty path means the user cancelled,
        // which must not then open a second picker at them.
        if (data.path) setValue(input, data.path);
        return;
      }
    } catch (_) { /* fall through to the in-app browser */ }

    open(input, wanted);
  }

  function directoryOf(value) {
    if (!value) return '';
    const cut = Math.max(value.lastIndexOf('\\'), value.lastIndexOf('/'));
    return cut > 0 ? value.slice(0, cut) : '';
  }

  function setValue(input, path) {
    input.value = path;
    // Anything watching the field — validation, the connect button — is
    // listening for input, not for assignment.
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // -------------------------------------------------------------------------
  // The in-app browser
  // -------------------------------------------------------------------------

  function build() {
    overlay = document.createElement('div');
    overlay.id = 'filepicker-overlay';
    overlay.className = 'modal-overlay hidden';

    overlay.innerHTML = `
      <div id="filepicker-modal">
        <div class="panel-header">
          <h2 class="panel-title" id="filepicker-title">Choose a file</h2>
          <button type="button" class="icon-btn" id="filepicker-close" aria-label="Close">
            <span class="material-symbols-outlined">close</span>
          </button>
        </div>
        <div id="filepicker-bar">
          <button type="button" id="filepicker-up" class="btn-secondary">
            <span class="material-symbols-outlined">upload</span>
            Up
          </button>
          <span id="filepicker-path"></span>
        </div>
        <div id="filepicker-list"></div>
        <div id="filepicker-actions">
          <button type="button" id="filepicker-cancel" class="btn-secondary">Cancel</button>
          <button type="button" id="filepicker-choose" class="btn-primary" disabled>Choose</button>
        </div>
      </div>`;

    document.body.appendChild(overlay);

    listEl    = overlay.querySelector('#filepicker-list');
    pathEl    = overlay.querySelector('#filepicker-path');
    upBtn     = overlay.querySelector('#filepicker-up');
    chooseBtn = overlay.querySelector('#filepicker-choose');
    titleEl   = overlay.querySelector('#filepicker-title');

    overlay.querySelector('#filepicker-close').addEventListener('click', close);
    overlay.querySelector('#filepicker-cancel').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    upBtn.addEventListener('click', () => { if (upBtn.dataset.parent) load(upBtn.dataset.parent); });
    chooseBtn.addEventListener('click', () => {
      if (selected && onChoose) onChoose(selected);
      close();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });
  }

  function open(input, wanted) {
    mode = wanted === 'folder' ? 'folder' : 'file';
    selected = '';
    // In folder mode the current folder is always a valid answer, so Choose
    // starts enabled: "the one I am looking at" is how people pick a folder.
    chooseBtn.disabled = mode === 'file';
    chooseBtn.textContent = mode === 'folder' ? 'Use this folder' : 'Choose';
    titleEl.textContent = mode === 'folder' ? 'Choose a folder' : 'Choose a file';
    onChoose = (path) => setValue(input, path);
    overlay.classList.remove('hidden');
    load(mode === 'folder' ? input.value : directoryOf(input.value));
  }

  function close() {
    overlay.classList.add('hidden');
    onChoose = null;
  }

  async function load(path) {
    listEl.innerHTML = '<div class="filepicker-empty">Loading…</div>';
    try {
      const res = await fetch(`/api/local/browse?path=${encodeURIComponent(path || '')}`);
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not read that folder');
      render(await res.json());
    } catch (e) {
      listEl.innerHTML = '';
      const err = document.createElement('div');
      err.className = 'filepicker-empty';
      err.textContent = e.message;
      listEl.appendChild(err);
    }
  }

  function render(data) {
    currentPath = data.path;
    pathEl.textContent = data.path;
    upBtn.dataset.parent = data.parent || '';
    upBtn.disabled = !data.parent;

    // Walking into a folder is how it gets picked.
    if (mode === 'folder') {
      selected = data.path;
      chooseBtn.disabled = !data.path;
    }

    listEl.innerHTML = '';
    if (!data.entries.length) {
      const empty = document.createElement('div');
      empty.className = 'filepicker-empty';
      empty.textContent = 'This folder is empty.';
      listEl.appendChild(empty);
      return;
    }

    data.entries.forEach(entry => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'filepicker-row' + (entry.is_dir ? ' filepicker-dir' : '');
      row.dataset.path = entry.path;

      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.textContent = entry.is_dir ? 'folder' : 'description';

      const name = document.createElement('span');
      name.className = 'filepicker-name';
      // textContent — a file name is not ours to trust as markup.
      name.textContent = entry.name;

      row.append(icon, name);

      if (!entry.is_dir) {
        const size = document.createElement('span');
        size.className = 'filepicker-size';
        size.textContent = `${entry.size.toLocaleString()} B`;
        row.appendChild(size);
      }

      row.addEventListener('click', () => {
        if (entry.is_dir) { load(entry.path); return; }
        if (mode === 'folder') return;   // files are not answers here
        selected = entry.path;
        chooseBtn.disabled = false;
        listEl.querySelectorAll('.filepicker-row').forEach(r =>
          r.classList.toggle('selected', r === row));
      });

      row.addEventListener('dblclick', () => {
        if (entry.is_dir || mode === 'folder') return;
        selected = entry.path;
        if (onChoose) onChoose(selected);
        close();
      });

      listEl.appendChild(row);
    });
  }

  window.shellmateFilePicker = { browseFor };
})();
