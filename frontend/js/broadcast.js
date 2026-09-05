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
    const backBtn = document.getElementById('broadcast-back');
    if (backBtn) backBtn.addEventListener('click', goBack);
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

  /**
   * Where to go back to when this closes (#283).
   *
   * Opening Broadcast from Settings had to close Settings — two overlays at
   * one level leave the one behind unreachable — so the trip was one-way and
   * whatever you were reading was gone. Set by the caller; consumed once.
   */
  let returnTo = null;

  async function open(opts) {
    returnTo = (opts && opts.returnTo) || null;
    const back = document.getElementById('broadcast-back');
    if (back) {
      back.classList.toggle('hidden', !returnTo);
      back.textContent = returnTo ? `Back to ${returnTo.label}` : '';
    }

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

  function close() {
    overlay.classList.add('hidden');
    // Only when asked for, and only once: closing by Escape or the × from a
    // Settings visit should not reopen Settings a second time later.
    const back = returnTo;
    returnTo = null;
    const backBtn = document.getElementById('broadcast-back');
    if (backBtn) backBtn.classList.add('hidden');
    return back;
  }

  /** Close and return to whatever opened this (#283). */
  function goBack() {
    const back = close();
    if (back && typeof back.open === 'function') back.open();
  }

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

  /** Vendor groups the user has opened (#366). Every library action —
   *  toggling a bolt, deleting, saving — re-renders the list, and rebuilding
   *  the <details> closed folded the group under the pointer mid-use. */
  const openGroups = new Set();

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
    iosxr:    'Cisco IOS-XR',
    fortios:  'Fortinet FortiOS',
    routeros: 'MikroTik RouterOS',
    huawei:   'Huawei VRP',
    aoscx:    'Aruba AOS-CX',
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
      // All closed (#284) until the user opens one — and then it *stays*
      // open across re-renders (#366), or clicking a bolt folded the group
      // being worked in.
      group.open = openGroups.has(platform);
      group.addEventListener('toggle', () => {
        if (group.open) openGroups.add(platform);
        else openGroups.delete(platform);
      });

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

    // Right-click edits in place (#368). Until now changing a saved command
    // meant deleting it and typing it again — or hand-editing snippets.json.
    row.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      editSnippet(snippet);
    });
    load.title += '\nRight-click to edit.';

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

    // Run it with the assistant (#552): the same commands, but walked one
    // approved step at a time with the model reading each result — as
    // against Broadcast, which fires them all and interprets nothing.
    const walk = document.createElement('button');
    walk.type = 'button';
    walk.className = 'snippet-walk';
    walk.title = 'Run this with the assistant, one approved step at a time';
    const walkIcon = document.createElement('span');
    walkIcon.className = 'material-symbols-outlined';
    walkIcon.textContent = 'smart_toy';
    walk.appendChild(walkIcon);
    walk.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!window.shellmateChat || !window.shellmateChat.startRunbook) return;
      close();
      window.shellmateChat.startRunbook(snippet);
    });

    row.append(load, walk, quick, add, del);
    return row;
  }

  /**
   * Edit a saved command in place (#368).
   *
   * The dialog's fields are single-line, so the commands — the one part that
   * is genuinely multi-line — go in as caller-built content, the same route
   * the icon picker takes. Everything not on the form (quick, send_return)
   * is carried over unchanged: an edit must not knock a command off the tab
   * menu as a side effect.
   */
  async function editSnippet(snippet) {
    const wrap = document.createElement('div');
    wrap.className = 'sm-dialog-field';
    const label = document.createElement('label');
    label.className = 'sm-dialog-label';
    label.textContent = 'Commands, one per line';
    const box = document.createElement('textarea');
    box.className = 'sm-dialog-input';
    box.rows = Math.min(10, Math.max(3, snippet.commands.length + 1));
    box.spellcheck = false;
    box.value = snippet.commands.join('\n');
    wrap.append(label, box);

    const answer = await window.shellmateDialog.form({
      title: 'Edit "' + snippet.name + '"',
      content: wrap,
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Name', required: true, value: snippet.name },
        { name: 'description', label: 'Description',
          value: snippet.description || '' },
        { name: 'platform', label: 'Vendor', type: 'select',
          value: snippet.platform || '',
          options: Object.entries(VENDOR)
            .map(([value, label2]) => ({ value, label: label2 })) },
        { name: 'wait_ms', label: 'Wait between commands (ms)',
          value: String(snippet.wait_ms ?? 500) },
      ],
    });
    if (!answer) return;

    const commands = box.value.split('\n').map(l => l.trim()).filter(Boolean);
    if (!commands.length) {
      report('error', 'A library entry needs at least one command.');
      return;
    }

    try {
      const res = await fetch('/api/snippets/' + encodeURIComponent(snippet.id), {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          name: answer.name,
          commands,
          description: answer.description || '',
          platform: answer.platform || '',
          wait_ms: parseInt(answer.wait_ms, 10) || 0,
          writes: commands.some(looksLikeAWrite),
          quick: snippet.quick,
          send_return: snippet.send_return !== false,
        }),
      });
      if (!res.ok) { report('error', (await res.json()).detail || 'Could not save it.'); return; }
      // The tab menu shows quick commands by name; a rename must reach it.
      window.dispatchEvent(new CustomEvent('shellmate:snippets-changed'));
      await loadLibrary();
      report('ok', `Updated "${answer.name}".`);
    } catch (e) {
      report('error', `Could not save it: ${e.message}`);
    }
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

    // The vendor is offered at save time (#367), because that is when the
    // person knows what they were sending it to — filed under "Any device"
    // it would sit in the one group that grows without bound.
    const answer = await window.shellmateDialog.form({
      title: 'Save to the library',
      list: commands.map(c => ({ text: c, mono: true })),
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Call it', required: true,
          value: commands[0].slice(0, 40) },
        { name: 'platform', label: 'Vendor', type: 'select',
          hint: 'Which group of the library it is filed under.',
          options: Object.entries(VENDOR)
            .map(([value, label]) => ({ value, label })) },
      ],
    });
    if (!answer || !answer.name) return;

    try {
      const res = await fetch('/api/snippets/new', {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          name: answer.name,
          commands,
          platform: answer.platform || '',
          wait_ms: waitMs(),
          // Flagged from what it looks like, so the library shows a warning on
          // the ones that change something. The user can correct it by editing
          // snippets.json; guessing here is better than never marking any.
          writes: commands.some(looksLikeAWrite),
        }),
      });
      if (!res.ok) { report('error', (await res.json()).detail || 'Could not save.'); return; }
      await loadLibrary();
      report('ok', `Saved "${answer.name}" to the library.`);
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

      // Only the last command is collected, and only when asked. A sequence
      // is usually setup-then-the-one-that-answers — `terminal length 0`
      // followed by `show version` — and collecting every line of it would
      // bury the reply somebody actually wanted under the ones they did not.
      if (collectWanted() && data.sent) {
        await collectReplies(commands[commands.length - 1],
                             data.results.filter(r => r.ok).map(r => r.session_id),
                             data.sent_at);
      }
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


  // -------------------------------------------------------------------------
  // Collecting the replies (#529)
  //
  // Sending to forty devices and being told to watch forty tabs is why the
  // Netmiko script gets written instead. The value is correlation, not
  // aggregation: "which of these six has the neighbour down".
  //
  // Three rules the rendering keeps to:
  //
  // **A device that did not answer is shown, not omitted.** Timeouts and
  // unrecognised prompts are rows of their own. An absent row would read as
  // agreement, which is the one wrong answer this must never give.
  //
  // **The baseline is named.** "Different from sw-01" is a fact somebody can
  // go and check. "Different from the consensus" is a claim this has no
  // standing to make — six upgraded and thirty-four not is a majority that
  // is wrong.
  //
  // **The ones that differ come first.** Everything on this list is there to
  // be scanned, and the two rows worth reading should not be at the bottom
  // of thirty-eight that say the same thing.
  // -------------------------------------------------------------------------

  /** The last collection, kept so the buttons under it have something to act on. */
  let collection = null;

  /** How each state reads, and how it sorts. Lower sorts first. */
  const STATE_TEXT = {
    differs:        ['differs', 0],
    timeout:        ['no reply in time', 1],
    'not-captured': ['prompt not recognised', 2],
    gone:           ['session gone', 3],
    baseline:       ['baseline', 4],
    identical:      ['same', 5],
  };

  function collectWanted() {
    const box = document.getElementById('broadcast-collect');
    return !!(box && box.checked);
  }

  /**
   * Ask the server to wait for the replies, then render them.
   *
   * `sent_at` comes back from the broadcast and goes straight out again
   * untouched. It is the server's own clock: a laptop four minutes fast
   * would otherwise discard every reply it got as too old.
   */
  async function collectReplies(command, ids, sentAt) {
    const waiting = document.createElement('div');
    waiting.className = 'broadcast-result broadcast-muted';
    waiting.textContent = `Waiting for ${ids.length} device`
      + `${ids.length === 1 ? '' : 's'} to finish answering "${command}"…`;
    results.appendChild(waiting);

    try {
      const res = await fetch('/api/broadcast/collect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: ids, command, sent_at: sentAt }),
      });
      const data = await res.json();
      waiting.remove();
      if (!res.ok) {
        report('error', data.detail || 'Could not collect the replies.');
        return;
      }
      collection = data;
      renderCollection(data);
    } catch (e) {
      waiting.remove();
      report('error', `Could not collect the replies: ${e.message || e}`);
    }
  }

  /** Which of the compare buckets a result landed in. */
  function stateOf(row, comparison) {
    if (row.state !== 'collected') return row.state;
    if (row.label === comparison.baseline) return 'baseline';
    return (comparison.differing || []).includes(row.label)
      ? 'differs' : 'identical';
  }

  function renderCollection(data) {
    const comparison = data.comparison || {};
    const block = document.createElement('div');
    block.className = 'broadcast-collect-block';

    const summary = document.createElement('div');
    summary.className = 'broadcast-collect-summary';
    summary.textContent = comparison.summary || '';
    block.appendChild(summary);

    const rows = (data.results || []).slice().sort((a, b) =>
      (STATE_TEXT[stateOf(a, comparison)] || ['', 9])[1]
      - (STATE_TEXT[stateOf(b, comparison)] || ['', 9])[1]);

    const list = document.createElement('div');
    list.className = 'broadcast-collect-list';
    rows.forEach(row => list.appendChild(deviceRow(row, comparison)));
    block.appendChild(list);

    block.appendChild(collectActions(data));
    results.appendChild(block);
  }

  function deviceRow(row, comparison) {
    const state = stateOf(row, comparison);
    const text = (STATE_TEXT[state] || [row.state])[0];

    const item = document.createElement('details');
    item.className = 'broadcast-device';
    item.dataset.state = state;
    // Open the ones worth reading. Thirty-eight identical replies expanded
    // is a wall nobody scrolls through; the two that differ are the answer.
    item.open = state === 'differs';

    const head = document.createElement('summary');
    const name = document.createElement('span');
    name.className = 'broadcast-device-name';
    name.textContent = row.label || row.session_id;
    const chip = document.createElement('span');
    chip.className = `broadcast-device-chip broadcast-chip-${state}`;
    chip.textContent = state === 'differs'
      ? `differs from ${comparison.baseline}` : text;
    head.append(name, chip);
    item.appendChild(head);

    const body = document.createElement('pre');
    body.className = 'broadcast-device-output';
    // The detail rather than an empty box: "no reply in time" with nothing
    // under it leaves somebody wondering whether the panel is still working.
    body.textContent = row.output || row.detail || '(nothing was captured)';
    item.appendChild(body);
    return item;
  }

  /** What can be done with a collection once it is on the screen. */
  function collectActions(data) {
    const bar = document.createElement('div');
    bar.className = 'broadcast-collect-actions';

    bar.appendChild(actionButton('content_copy', 'Copy as text', async () => {
      try {
        await navigator.clipboard.writeText(collectionText(data));
        report('ok', 'Copied.');
      } catch (e) {
        report('error', 'The browser would not give access to the clipboard.');
      }
    }));

    bar.appendChild(actionButton('download', 'Save as file', () => {
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
      const blob = new Blob([collectionText(data)], { type: 'text/plain' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `broadcast-${stamp}.txt`;
      link.click();
      // Revoked on a later tick rather than in this one: the click is
      // asynchronous in every browser, and a URL revoked in the same frame
      // gives a download of zero bytes.
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    }));

    bar.appendChild(actionButton('smart_toy', 'Compare with the assistant',
                                 () => askTheAssistant(data)));
    return bar;
  }

  function actionButton(icon, label, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-tertiary';
    const glyph = document.createElement('span');
    glyph.className = 'material-symbols-outlined';
    glyph.textContent = icon;
    button.append(glyph, document.createTextNode(` ${label}`));
    button.addEventListener('click', onClick);
    return button;
  }

  /**
   * The collection as plain text — for the clipboard, the file and the
   * assistant alike.
   *
   * One renderer for all three. Three would be three chances for the file
   * somebody keeps as evidence to say something the screen did not.
   */
  function collectionText(data) {
    const comparison = data.comparison || {};
    const lines = [`Command: ${data.command}`, ''];
    if (comparison.summary) lines.push(comparison.summary, '');
    (data.results || []).forEach(row => {
      const state = stateOf(row, comparison);
      const text = (STATE_TEXT[state] || [row.state])[0];
      lines.push(`--- ${row.label || row.session_id} (${text}) ---`);
      lines.push(row.output || row.detail || '(nothing was captured)');
      lines.push('');
    });
    return lines.join('\n');
  }

  /**
   * Hand the collection to the chat panel as an attachment.
   *
   * As an attachment rather than a message: it arrives under a heading that
   * says each block is a different device, which is the one thing a model
   * reading forty near-identical outputs will otherwise get wrong — it
   * merges them and answers about a device that does not exist.
   */
  function askTheAssistant(data) {
    const chat = window.shellmateChat;
    if (!chat || typeof chat.attachComparison !== 'function') {
      report('error', 'The chat panel is not available.');
      return;
    }
    close();
    chat.attachComparison(collectionText(data), data.command);
  }

  window.openBroadcast = open;
})();
