/**
 * setup.js — Taking a setup somewhere else (#563).
 *
 * Three things in one Settings section: export a bundle, import one, and
 * move the data folder.
 *
 * The import is a **preview first**, always. A bundle is a file somebody
 * was sent, and the difference between "31 connections, 4 of which you
 * already have" and "profiles.json" is the difference between a decision
 * and a leap. Nothing is applied until the table has been looked at and a
 * choice made per file.
 *
 * The DPAPI sentence appears in three places here, and that is deliberate
 * rather than sloppy: saved passwords do not travel, and somebody who
 * exports their setup, moves machines, and finds their credentials gone
 * has been failed by that sentence being somewhere they did not read.
 */
(function () {
  'use strict';

  /** The bundle chosen for import, base64, and what inspect() said about it. */
  let chosen = null;
  let preview = null;

  function el(id) { return document.getElementById(id); }

  function init() {
    if (!el('setup-export')) return;

    el('setup-export').addEventListener('click', doExport);
    el('setup-choose').addEventListener('click', chooseBundle);
    el('setup-apply').addEventListener('click', doApply);
    el('setup-move-choose').addEventListener('click', chooseFolder);
    el('setup-move-go').addEventListener('click', doMove);

    loadParts();
  }

  // -------------------------------------------------------------------------
  // Export
  // -------------------------------------------------------------------------

  async function loadParts() {
    let data;
    try {
      data = await (await fetch('/api/setup/parts')).json();
    } catch (_) {
      return;
    }

    const host = el('setup-parts');
    host.innerHTML = '';
    (data.parts || []).forEach(part => {
      const row = document.createElement('label');
      row.className = 'setup-part';

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.key = part.key;
      // Present and not optional is ticked; the licence is not, because a
      // licence key is the one thing here somebody could send onward
      // without meaning to.
      box.checked = part.present && !part.optional;
      box.disabled = !part.present;

      const text = document.createElement('span');
      text.className = 'setup-part-text';
      const name = document.createElement('strong');
      name.textContent = part.label + (part.present ? '' : ' — none saved');
      const why = document.createElement('span');
      why.className = 'setup-part-why';
      why.textContent = part.describe;
      text.append(name, why);

      row.append(box, text);
      host.appendChild(row);
    });

    // The promise, rendered from the server's own list rather than typed
    // into the page — so what is claimed and what is enforced cannot drift.
    const never = el('setup-never');
    if (never) {
      never.textContent = 'Never included, whatever is ticked: '
        + (data.never || []).join(', ') + '.';
    }
  }

  async function doExport() {
    const include = [...document.querySelectorAll('#setup-parts input:checked')]
      .map(box => box.dataset.key);
    if (!include.length) {
      say('setup-export-status', 'Tick at least one thing to export.', true);
      return;
    }

    say('setup-export-status', 'Building…');
    try {
      const res = await fetch('/api/setup/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `shellmate-setup-${new Date().toISOString().slice(0, 10)}.zip`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      say('setup-export-status',
          'Saved. Saved passwords are not in it — see the note below.');
    } catch (e) {
      say('setup-export-status', String(e.message || e), true);
    }
  }

  // -------------------------------------------------------------------------
  // Import
  // -------------------------------------------------------------------------

  function chooseBundle() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.zip';
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        // Base64 of the bytes, read here rather than uploaded as multipart
        // — the same path a vault backup takes, so there is one way a file
        // the user picked reaches the server.
        const bytes = new Uint8Array(reader.result);
        let binary = '';
        bytes.forEach(b => { binary += String.fromCharCode(b); });
        chosen = btoa(binary);
        inspectBundle();
      };
      reader.readAsArrayBuffer(file);
    });
    input.click();
  }

  async function inspectBundle() {
    say('setup-import-status', 'Reading…');
    try {
      const res = await fetch('/api/setup/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: chosen }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      preview = data;
      renderPreview(data);
      say('setup-import-status',
          `Exported ${data.created || 'at an unknown time'}`
          + `${data.version ? ` by ${data.version}` : ''}. `
          + 'Nothing has been changed yet.');
    } catch (e) {
      preview = null;
      el('setup-preview').innerHTML = '';
      el('setup-apply').classList.add('hidden');
      say('setup-import-status', String(e.message || e), true);
    }
  }

  function renderPreview(data) {
    const host = el('setup-preview');
    host.innerHTML = '';

    const table = document.createElement('table');
    table.className = 'setup-table';
    const head = document.createElement('tr');
    ['What', 'In the bundle', 'What to do'].forEach(label => {
      const th = document.createElement('th');
      th.textContent = label;
      head.appendChild(th);
    });
    table.appendChild(head);

    (data.parts || []).forEach(part => {
      const row = document.createElement('tr');

      const what = document.createElement('td');
      const name = document.createElement('strong');
      name.textContent = part.label || part.key;
      const why = document.createElement('div');
      why.className = 'setup-part-why';
      why.textContent = part.describe || '';
      what.append(name, why);

      const count = document.createElement('td');
      count.textContent = summarise(part);
      if (part.checksum_ok === false) {
        count.classList.add('setup-bad');
        count.textContent += ' — the checksum does not match, so this file '
          + 'has changed since it was exported.';
      }

      const choice = document.createElement('td');
      const select = document.createElement('select');
      select.className = 'setting-input';
      select.dataset.key = part.key;
      const options = part.known
        ? (part.mergeable
           // Merge first, and the default: somebody importing a
           // colleague's setup has their own corrections in these files,
           // and replace as the default would quietly discard them.
           ? [['merge', 'Add what is missing'],
              ['replace', 'Replace mine entirely'], ['skip', 'Skip']]
           : [['skip', 'Skip'], ['replace', 'Replace mine entirely']])
        : [['skip', 'Skip']];
      options.forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        select.appendChild(option);
      });
      select.disabled = !part.known || part.error;
      choice.appendChild(select);

      row.append(what, count, choice);
      table.appendChild(row);
    });

    host.appendChild(table);
    el('setup-apply').classList.remove('hidden');
  }

  function summarise(part) {
    if (part.error) return part.error;
    if (part.count === null || part.count === undefined) {
      return `${Math.round((part.bytes || 0) / 1024)} KB`;
    }
    const overlap = (part.overlap === null || part.overlap === undefined)
      ? '' : `, ${part.overlap} of which you already have`;
    return `${part.count} item${part.count === 1 ? '' : 's'}${overlap}`;
  }

  async function doApply() {
    if (!chosen || !preview) return;

    const actions = {};
    document.querySelectorAll('#setup-preview select').forEach(select => {
      actions[select.dataset.key] = select.value;
    });

    const doing = Object.entries(actions).filter(([, a]) => a !== 'skip');
    if (!doing.length) {
      say('setup-import-status', 'Everything is set to Skip.', true);
      return;
    }

    // Listed by name, and the replacements named as replacements. This is
    // the moment somebody is about to overwrite their own connections with
    // a colleague's, and a confirmation that said "Import 5 files?" would
    // not have told them.
    const ok = await window.shellmateDialog.confirm({
      title: 'Import this setup?',
      list: doing.map(([key, action]) => {
        const part = (preview.parts || []).find(p => p.key === key) || {};
        return {
          text: part.label || key,
          detail: action === 'replace' ? 'replaces yours' : 'adds what is missing',
        };
      }),
      body: 'Anything set to Replace overwrites what you have now. There is '
          + 'no undo, so export your own setup first if you want one.',
      note: doing.some(([, a]) => a === 'replace')
        ? 'Some of these replace rather than add to what you have.' : '',
      confirmLabel: 'Import',
      danger: doing.some(([, a]) => a === 'replace'),
    });
    if (!ok) return;

    say('setup-import-status', 'Importing…');
    try {
      const res = await fetch('/api/setup/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: chosen, actions }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      say('setup-import-status',
          `Imported: ${(data.applied || []).map(a => a.label).join(', ')}. `
          + 'Reload the page to see all of it.');
      if (typeof window.reloadSettings === 'function') window.reloadSettings();
    } catch (e) {
      say('setup-import-status', String(e.message || e), true);
    }
  }

  // -------------------------------------------------------------------------
  // Moving the data folder
  // -------------------------------------------------------------------------

  async function chooseFolder() {
    let picked = '';
    try {
      // The one picker every other folder field uses. `available: false`
      // means there is no native window, which is not an error — the box
      // can be typed into.
      const res = await fetch('/api/pick-file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: 'Move the ShellMate data folder to…', folder: true,
        }),
      });
      picked = (await res.json()).path || '';
    } catch (_) { /* the box can be typed into */ }
    if (picked) el('setup-move-target').value = picked;
    planMove();
  }

  async function planMove() {
    const target = (el('setup-move-target').value || '').trim();
    if (!target) return;
    try {
      const res = await fetch('/api/setup/move/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target }),
      });
      const plan = await res.json();
      const size = plan.bytes
        ? ` (${Math.max(1, Math.round(plan.bytes / 1024 / 1024))} MB to copy)`
        : '';
      say('setup-move-status',
          (plan.problems || []).join(' ')
          || `Ready: ${plan.from} → ${plan.to}${size}`,
          !plan.ok);
    } catch (e) {
      say('setup-move-status', String(e.message || e), true);
    }
  }

  async function doMove() {
    const target = (el('setup-move-target').value || '').trim();
    if (!target) {
      say('setup-move-status', 'Choose a folder first.', true);
      return;
    }

    const ok = await window.shellmateDialog.confirm({
      title: 'Move the data folder?',
      body: 'Everything is copied to the new folder and ShellMate is pointed '
          + 'at it from now on. Your current folder is left exactly as it '
          + 'is — nothing is deleted — so you can go back by removing '
          + 'data-dir.txt beside the executable.',
      note: 'ShellMate has to restart afterwards.',
      confirmLabel: 'Move it',
    });
    if (!ok) return;

    say('setup-move-status', 'Copying…');
    try {
      const res = await fetch('/api/setup/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      say('setup-move-status', `${data.note} Restart ShellMate to use it.`);
    } catch (e) {
      say('setup-move-status', String(e.message || e), true);
    }
  }

  // -------------------------------------------------------------------------

  function say(id, text, bad) {
    const node = el(id);
    if (!node) return;
    node.textContent = text;
    node.className = 'settings-section-hint' + (bad ? ' setup-bad' : '');
  }

  document.addEventListener('DOMContentLoaded', init);
})();
