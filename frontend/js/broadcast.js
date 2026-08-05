/**
 * broadcast.js — Send commands to several devices at once.
 *
 * Compose-and-send rather than keystroke mirroring. Mirroring what you type
 * into every open tab is the usual implementation and it is the wrong one for
 * this: a stray keypress reaches the whole fleet, and you never see the
 * finished command before it lands. Here the commands are written once, the
 * targets are listed by name, and the result of each device is reported
 * separately — so one that was disconnected shows up as a failure rather than
 * being quietly skipped.
 *
 * Four things it has to do well:
 *
 *  - **Pick targets** in the same shape as the assistant's session picker,
 *    because they answer the same question and should not need learning twice.
 *  - **Remember commands worth repeating.** A broadcast is the worst possible
 *    place to improvise, so the library is where the risky ones live, written
 *    once and checked.
 *  - **Run sequences.** Save then verify; set then show. With a wait, because
 *    a device that has just written its configuration will not answer for a
 *    second or two.
 *  - **Take a block of lines**, so a short piece of configuration can be
 *    pushed without pasting it into each tab.
 *
 * The confirmation shows every command against every device before anything is
 * sent. That is the whole safety model, and it is deliberately unskippable.
 */
(function () {
  'use strict';

  /** Shorthand for a Stockton value. */
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

  let overlay, input, targets, results, waitInput, libraryEl, searchEl;
  let library = [];
  /** Session ids the user has ticked. */
  let selected = new Set();

  document.addEventListener('DOMContentLoaded', () => {
    overlay   = document.getElementById('broadcast-overlay');
    input     = document.getElementById('broadcast-command');
    targets   = document.getElementById('broadcast-targets');
    results   = document.getElementById('broadcast-results');
    waitInput = document.getElementById('broadcast-wait');
    // The panel opens at the configured default rather than a literal.
    if (waitInput) waitInput.value = A('broadcast.default_wait', 500);
    libraryEl = document.getElementById('broadcast-library');
    searchEl  = document.getElementById('broadcast-library-search');
    if (!overlay) return;

    document.getElementById('sidebar-link-broadcast')
      .addEventListener('click', (e) => { e.preventDefault(); open(); });
    document.getElementById('broadcast-close').addEventListener('click', close);
    document.getElementById('broadcast-send').addEventListener('click', send);
    // One control for one axis. "None" was a second button doing half of
    // what this one now does, and two controls for one thing is how people
    // end up unsure which they pressed.
    document.getElementById('broadcast-all').addEventListener('click', toggleAll);
    document.getElementById('broadcast-save').addEventListener('click', saveToLibrary);

    searchEl.addEventListener('input', renderLibrary);

    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
      // Ctrl+Shift+B is the usual shortcut for this in terminal tools.
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        overlay.classList.contains('hidden') ? open() : close();
      }
    });

    input.addEventListener('input', updateSummary);

    // Enter inserts a newline here, unlike the single-line original: this box
    // takes a block of commands, so Enter has to mean "next command". Sending
    // is Ctrl+Enter or the button.
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });
  });

  function openTabs() {
    return (typeof window.getOpenTabs === 'function') ? window.getOpenTabs() : [];
  }

  async function open() {
    // Default to the active tab, which is what someone opening this while
    // looking at a device almost always means.
    if (!selected.size) {
      const active = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
      if (active) selected.add(active.sessionId);
    }
    // A search left behind by the last visit filtered the next one down to
    // "Nothing matches." — which read as the library being hidden (#239).
    if (searchEl) searchEl.value = '';
    renderTargets();
    results.innerHTML = '';
    overlay.classList.remove('hidden');
    await loadLibrary();
    updateSummary();
    updateSelectAll();
    setTimeout(() => input.focus(), 50);
  }

  function close() { overlay.classList.add('hidden'); }

  // -------------------------------------------------------------------------
  // Targets
  // -------------------------------------------------------------------------

  function renderTargets() {
    const tabs = openTabs();
    targets.innerHTML = '';

    // A tab closed since last time must not stay selected, or a broadcast
    // would name a device that is no longer there.
    const live = new Set(tabs.map(t => t.sessionId));
    [...selected].forEach(id => { if (!live.has(id)) selected.delete(id); });

    if (!tabs.length) {
      const empty = document.createElement('div');
      empty.className = 'broadcast-empty';
      empty.textContent = 'No sessions open.';
      targets.appendChild(empty);
      return;
    }

    tabs.forEach((tab, index) => {
      const row = document.createElement('label');
      row.className = 'broadcast-target' + (tab.isConnected ? '' : ' broadcast-target-down');

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.sessionId = tab.sessionId;
      // A disconnected session cannot receive anything, so it starts unticked
      // rather than silently failing when Send is pressed.
      box.checked = tab.isConnected && selected.has(tab.sessionId);
      box.disabled = !tab.isConnected;
      box.addEventListener('change', () => {
        box.checked ? selected.add(tab.sessionId) : selected.delete(tab.sessionId);
        updateSummary();
        updateSelectAll();
      });

      const num = document.createElement('span');
      num.className = 'broadcast-target-num';
      num.textContent = String(index + 1);

      const name = document.createElement('span');
      name.className = 'broadcast-target-name';
      name.textContent = tab.label || tab.hostname || tab.sessionId.slice(0, 8);

      const kind = document.createElement('span');
      kind.className = 'broadcast-kind';
      kind.textContent = tab.isConnected
        ? (tab.connectionType || 'ssh').toUpperCase()
        : 'DISCONNECTED';

      row.append(box, num, name, kind);
      targets.appendChild(row);
    });
  }

  /** The boxes that can actually be ticked — disconnected sessions cannot. */
  function selectableBoxes() {
    return [...targets.querySelectorAll('input[type=checkbox]:not(:disabled)')];
  }

  function setAll(state) {
    selectableBoxes().forEach(box => {
      box.checked = state;
      state ? selected.add(box.dataset.sessionId) : selected.delete(box.dataset.sessionId);
    });
    updateSummary();
    updateSelectAll();
  }

  /**
   * "All" toggles rather than only selecting.
   *
   * Pressing a select-all twice and getting nothing back is a small
   * surprise, but it is a surprise in the one panel where the selection
   * decides which devices receive a command.
   *
   * Compared against the *selectable* boxes, not all of them: one
   * disconnected session in the list would otherwise mean the panel never
   * counted as fully selected, and the toggle would never flip.
   */
  function toggleAll() {
    const boxes = selectableBoxes();
    if (!boxes.length) return;
    setAll(!boxes.every(box => box.checked));
  }

  /** Keep the button saying what pressing it will do. */
  function updateSelectAll() {
    const button = document.getElementById('broadcast-all');
    if (!button) return;

    const boxes = selectableBoxes();
    const allOn = boxes.length > 0 && boxes.every(box => box.checked);

    button.textContent = allOn ? 'None' : 'All';
    button.title = boxes.length
      ? (allOn ? 'Clear every selected device' : 'Select every connected device')
      : 'No connected sessions to select';
    // Nothing to select and nothing to clear is not two states worth
    // toggling between.
    button.disabled = !boxes.length;
  }

  function selectedIds() {
    return [...targets.querySelectorAll('input[type=checkbox]:checked')]
      .map(box => box.dataset.sessionId);
  }

  function selectedNames() {
    const names = {};
    targets.querySelectorAll('input[type=checkbox]:checked').forEach(box => {
      names[box.dataset.sessionId] =
        box.parentElement.querySelector('.broadcast-target-name').textContent;
    });
    return names;
  }

  // -------------------------------------------------------------------------
  // The library
  // -------------------------------------------------------------------------

  /** Whether the last library fetch failed — "empty" and "unreachable" are
   *  different sentences, and showing the first for the second read as the
   *  library being hidden (#239). */
  let libraryError = false;

  async function loadLibrary() {
    try {
      const res = await fetch('/api/snippets');
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      library = (await res.json()).snippets || [];
      libraryError = false;
    } catch (_) {
      library = [];
      libraryError = true;
    }
    renderLibrary();
  }

  /** Platform id -> heading. Anything unlisted falls back to the id itself. */
  const VENDOR = {
    '':       'Any device',
    ios:      'Cisco IOS / IOS-XE',
    nxos:     'Cisco NX-OS',
    asa:      'Cisco ASA',
    junos:    'Juniper Junos',
    panos:    'Palo Alto PAN-OS',
    arista:   'Arista EOS',
    linux:    'Linux / Unix shell',
  };

  /**
   * Group the library by vendor.
   *
   * Most of the library does not apply to most devices in a mixed estate, and
   * a flat list of a hundred and thirty says nothing about which is which.
   *
   * The platform of whatever is *selected* comes first, because that is what
   * you are about to send to. The rest stay visible rather than hidden:
   * hiding them is tidier and wrong the moment somebody wants a Junos command
   * while looking at a switch.
   */
  function groupLibrary(matches) {
    const groups = new Map();
    matches.forEach(s => {
      const key = s.platform || '';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(s);
    });

    const selectedPlatforms = new Set(
      selectedIds()
        .map(id => (window.getDeviceInfo ? window.getDeviceInfo(id) : null))
        .filter(Boolean)
        .map(info => info.platform)
        .filter(p => p && p !== 'generic'));

    return [...groups.entries()].sort((a, b) => {
      // Cross-vendor first, then whatever the selected devices are, then
      // alphabetically so the order never shuffles for no reason.
      const rank = (entry) => (entry[0] === '' ? 0 : selectedPlatforms.has(entry[0]) ? 1 : 2);
      const diff = rank(a) - rank(b);
      if (diff) return diff;
      return (VENDOR[a[0]] || a[0]).localeCompare(VENDOR[b[0]] || b[0]);
    });
  }

  function renderLibrary() {
    const query = (searchEl.value || '').trim().toLowerCase();
    libraryEl.innerHTML = '';

    const matches = library.filter(s =>
      !query ||
      s.name.toLowerCase().includes(query) ||
      (s.description || '').toLowerCase().includes(query) ||
      s.commands.join(' ').toLowerCase().includes(query));

    if (!matches.length) {
      const empty = document.createElement('div');
      empty.className = 'broadcast-empty';
      empty.textContent = libraryError
        ? 'Could not load the library — check that ShellMate is reachable and reopen the panel.'
        : (query ? 'Nothing matches.' : 'The library is empty.');
      libraryEl.appendChild(empty);
      return;
    }

    // Searching flattens the groups. The point of a search is finding
    // something without knowing which vendor it was filed under.
    if (query) {
      matches.forEach(s => libraryEl.appendChild(snippetRow(s, true)));
      return;
    }

    groupLibrary(matches).forEach((entry, index) => {
      const platform = entry[0];
      const items = entry[1];

      const group = document.createElement('details');
      group.className = 'snippet-group';
      // The first two open: cross-vendor, and whatever is selected. The rest
      // are one click away rather than a wall of a hundred rows.
      group.open = index < 2;

      const summary = document.createElement('summary');
      summary.className = 'snippet-group-head';

      const title = document.createElement('span');
      title.textContent = VENDOR[platform] || platform;

      const count = document.createElement('span');
      count.className = 'snippet-group-count';
      count.textContent = String(items.length);

      summary.append(title, count);
      group.appendChild(summary);

      items.forEach(s => group.appendChild(snippetRow(s, false)));
      libraryEl.appendChild(group);
    });
  }

  /**
   * One library entry.
   *
   * Clicking loads it. Ctrl-clicking, or the + button, *appends* it — so a
   * sequence can be assembled from pieces that have already been written and
   * checked rather than improvised into a box that sends to a fleet.
   */
  function snippetRow(snippet, showVendor) {
    const row = document.createElement('div');
    row.className = 'snippet-row';

    const load = document.createElement('button');
    load.type = 'button';
    load.className = 'snippet-load';
    load.title = (snippet.description ? snippet.description + '\n\n' : '')
      + snippet.commands.join('\n')
      + '\n\nClick to load, Ctrl+click to add to what is already there.';

    const name = document.createElement('span');
    name.className = 'snippet-name';
    // textContent — a snippet name may have been typed by the user.
    name.textContent = snippet.name;
    load.appendChild(name);

    if (showVendor && snippet.platform) {
      const tag = document.createElement('span');
      tag.className = 'snippet-tag snippet-vendor';
      tag.textContent = VENDOR[snippet.platform] || snippet.platform;
      load.appendChild(tag);
    }

    if (snippet.writes) {
      const tag = document.createElement('span');
      tag.className = 'snippet-tag snippet-writes';
      tag.textContent = 'writes';
      load.appendChild(tag);
    }
    if (snippet.commands.length > 1) {
      const tag = document.createElement('span');
      tag.className = 'snippet-tag';
      tag.textContent = snippet.commands.length + ' cmds';
      load.appendChild(tag);
    }

    load.addEventListener('click', (e) => {
      applySnippet(snippet, e.ctrlKey || e.metaKey || e.shiftKey);
    });

    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'snippet-add';
    add.title = 'Add to the commands already in the box';
    add.innerHTML = '<span class="material-symbols-outlined">add</span>';
    add.addEventListener('click', () => applySnippet(snippet, true));

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'snippet-delete';
    del.title = 'Remove from the library';
    del.innerHTML = '<span class="material-symbols-outlined">close</span>';
    del.addEventListener('click', async () => {
      const ok = await window.shellmateDialog.confirm({
        title: 'Remove "' + snippet.name + '" from the library?',
        body: 'Removing a built-in keeps it removed. Reset the library to bring it back.',
        confirmLabel: 'Remove',
        danger: true,
      });
      if (!ok) return;
      await fetch('/api/snippets/' + encodeURIComponent(snippet.id), { method: 'DELETE' });
      await loadLibrary();
    });

    // Mark it as a quick command — offered on a tab's right-click menu.
    // Here rather than in Settings because this *is* the library, and one
    // editor reached by every route beats two that can disagree.
    const quick = document.createElement('button');
    quick.type = 'button';
    quick.className = 'snippet-quick' + (snippet.quick ? ' on' : '');
    quick.title = snippet.quick
      ? 'On the tab right-click menu. Click to remove it.'
      : 'Add to the tab right-click menu';
    // textContent, not a concatenated innerHTML string: the icon scanners in
    // tools/vendor_assets.py and test_icons.py only see names written plainly,
    // and 'bolt' survived subsetting by luck rather than declaration.
    const quickIcon = document.createElement('span');
    quickIcon.className = 'material-symbols-outlined';
    quickIcon.textContent = 'bolt';
    quick.appendChild(quickIcon);
    quick.addEventListener('click', async (e) => {
      e.stopPropagation();
      await fetch('/api/snippets/' + encodeURIComponent(snippet.id), {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          name: snippet.name, commands: snippet.commands,
          description: snippet.description, platform: snippet.platform,
          wait_ms: snippet.wait_ms, writes: snippet.writes,
          quick: !snippet.quick,
          send_return: snippet.send_return !== false,
        }),
      });
      // The tab menu caches its list, so tell it rather than letting it go
      // stale until the next reload.
      window.dispatchEvent(new CustomEvent('shellmate:snippets-changed'));
      await loadLibrary();
    });

    row.append(load, quick, add, del);
    return row;
  }

  /**
   * Put a snippet into the command box.
   *
   * Appending takes the *largest* wait of the snippets involved rather than
   * the last one clicked. Waiting longer than necessary costs a second;
   * waiting less than a device needs means the next command lands while it is
   * still busy, which is the failure that matters.
   */
  function applySnippet(snippet, append) {
    const existing = append ? commandList() : [];
    const commands = existing.concat(snippet.commands);

    input.value = commands.join('\n');
    waitInput.value = append
      ? Math.max(parseInt(waitInput.value, 10) || 0, snippet.wait_ms)
      : snippet.wait_ms;

    updateSummary();
    input.focus();
    // Cursor to the end, so the next append visibly lands after this one.
    input.setSelectionRange(input.value.length, input.value.length);
    input.scrollTop = input.scrollHeight;
  }

  async function saveToLibrary() {
    const commands = commandList();
    if (!commands.length) { report('error', 'Type a command before saving it.'); return; }

    const name = await window.shellmateDialog.prompt({
      title: 'Save to the library',
      label: 'Call it',
      value: commands[0].slice(0, 40),
      list: commands.map(c => ({ text: c, mono: true })),
      confirmLabel: 'Save',
    });
    if (!name) return;

    try {
      const res = await fetch('/api/snippets/new', {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          name,
          commands,
          wait_ms: waitMs(),
          // Flagged from what it looks like, so the library shows a warning on
          // the ones that change something. The user can correct it by editing
          // snippets.json; guessing here is better than never marking any.
          writes: commands.some(looksLikeAWrite),
        }),
      });
      if (!res.ok) { report('error', (await res.json()).detail || 'Could not save.'); return; }
      await loadLibrary();
      report('ok', `Saved "${name}" to the library.`);
    } catch (e) {
      report('error', `Could not save: ${e.message}`);
    }
  }

  function looksLikeAWrite(command) {
    return /^\s*(wr|write|copy\s+run|commit|conf|configure|no\s|shut|reload|clear)\b/i
      .test(command);
  }

  // -------------------------------------------------------------------------
  // Sending
  // -------------------------------------------------------------------------

  /** Each non-blank line is one command. */
  function commandList() {
    return (input.value || '').split('\n').map(l => l.trim()).filter(Boolean);
  }

  function waitMs() {
    const value = parseInt(waitInput.value, 10);
    return Number.isFinite(value) ? Math.max(0, Math.min(60000, value)) : 500;
  }

  /** Keep the Send button honest about what it is about to do. */
  function updateSummary() {
    const summary = document.getElementById('broadcast-summary');
    if (!summary) return;
    const commands = commandList().length;
    const devices = selectedIds().length;
    summary.textContent = (!commands || !devices)
      ? ''
      : `${commands} command${commands === 1 ? '' : 's'} × ` +
        `${devices} device${devices === 1 ? '' : 's'}`;
  }

  async function send() {
    const commands = commandList();
    const ids = selectedIds();
    const names = selectedNames();

    results.innerHTML = '';
    if (!commands.length) { report('error', 'Type a command to send.'); return; }
    if (!ids.length) { report('error', 'Select at least one session.'); return; }

    // Sending to many devices at once is exactly the sort of thing that is
    // regretted afterwards, so show all of it — every command, every device —
    // and ask. The same names are reused in the results, so what is confirmed
    // and what is reported cannot disagree.
    //
    // This is the safety mechanism the whole feature is built on, which is
    // why it is no longer a native confirm(): that could only render the list
    // as newlines and two spaces, and could not be styled to look like it
    // mattered.
    const writes = commands.some(looksLikeAWrite);
    const ok = await window.shellmateDialog.confirm({
      title: `Send ${commands.length} command${commands.length === 1 ? '' : 's'} to ` +
             `${ids.length} device${ids.length === 1 ? '' : 's'}?`,
      list: [
        ...commands.map((c, i) => ({ text: c, detail: `${i + 1}`, mono: true })),
        ...ids.map(id => ({ text: names[id], detail: 'device' })),
      ],
      body: commands.length > 1
        ? `Sent in order on each device, ${waitMs()}ms apart. Devices run at the same time.`
        : '',
      note: writes
        ? 'One of these changes the device rather than only reading from it.'
        : '',
      confirmLabel: 'Send',
      danger: writes,
    });
    if (!ok) return;

    const button = document.getElementById('broadcast-send');
    button.disabled = true;
    report('muted', `Sending… watch the tabs to see it land.`);

    try {
      const res = await fetch('/api/broadcast', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          session_ids: ids, commands, wait_ms: waitMs(), execute: true,
        }),
      });
      const data = await res.json();
      results.innerHTML = '';
      if (!res.ok) { report('error', data.detail || 'Broadcast failed.'); return; }

      data.results.forEach(r => {
        const who = names[r.session_id] || r.label;
        if (r.ok) {
          report('ok', `${who}: sent ${r.sent.length} command${r.sent.length === 1 ? '' : 's'}`);
        } else {
          report('error', `${who}: ${r.error}`);
        }
      });
      report('muted', `${data.sent} of ${data.total} sent.`);
    } catch (e) {
      results.innerHTML = '';
      report('error', `Could not reach the server: ${e.message}`);
    } finally {
      button.disabled = false;
    }
  }

  function report(kind, text) {
    const row = document.createElement('div');
    row.className = `broadcast-result broadcast-${kind}`;
    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = kind === 'ok' ? 'check_circle' : kind === 'error' ? 'close' : 'list_alt';
    const label = document.createElement('span');
    // textContent — device labels and error text are not ours to trust.
    label.textContent = text;
    row.append(icon, label);
    results.appendChild(row);
  }

  window.openBroadcast = open;
})();
