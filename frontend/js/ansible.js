/**
 * ansible.js — Driving an Ansible runner, and watching it work (#585).
 *
 * ShellMate does not run Ansible. It talks to an `ansible-runner-service`
 * container through `/api/ansible/*` and shows what the container is doing:
 * what it holds, what a run is doing task by task, what changed, and what it
 * said when it failed.
 *
 * Three things shape this file, and all three come from the service rather
 * than from taste:
 *
 * - **The runner may be unreachable and the panel still has to work.** The
 *   playbooks endpoint answers with an `error` and an empty runner list
 *   instead of failing, precisely so somebody can go on writing a playbook
 *   while the container is down. Every list here therefore renders from
 *   whatever arrived rather than from an assumption that all of it did.
 *
 * - **Events are polled with `since`, not re-fetched.** The service returns
 *   every event it has, every time; `since` is the cheap path and the only
 *   one that stays cheap across a play with two thousand events in it.
 *
 * - **There is no upload API.** A playbook written here reaches the runner by
 *   being copied over an SSH session ShellMate already has to the machine
 *   hosting the container — a real limitation of the service, and one this
 *   panel states rather than papering over.
 */
(function () {
  'use strict';

  let overlay;

  /** How often a live run is asked what it is doing. */
  const POLL_MS = 1200;

  /** The run being watched, if any. */
  let live = null;         // { uuid, playbook, target, check, since, timer }
  /** Every event of that run, kept because the report is built from them. */
  let seenEvents = [];
  /** What the last status call said, for the report's summary line. */
  let lastState = null;
  /** Everything the estate offered last time the Run dialog was opened. */
  let estate = { groups: {}, hosts: [], hostvars: {}, skipped: [] };
  /** The playbook the Run dialog is about, and which list it came from. */
  let running = { name: '', source: 'runner' };
  /** Whether the last status call found a runner answering. */
  let runnerReady = false;

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('ansible-overlay');
    if (!overlay) return;

    document.getElementById('sidebar-link-ansible')
      .addEventListener('click', (e) => { e.preventDefault(); openAnsible(); });

    document.getElementById('ansible-close')
      .addEventListener('click', closeAnsible);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeAnsible();
    });

    document.getElementById('ansible-refresh')
      .addEventListener('click', () => { refreshStatus(); refreshPlaybooks(); });

    document.getElementById('ansible-open-settings')
      .addEventListener('click', () => {
        // Settings and this panel are overlays at the same level, so one has
        // to close for the other to be reachable — the same reason the Logs
        // panel closes before sending somebody to a setting.
        closeAnsible();
        if (typeof window.openSettingsSection === 'function') {
          window.openSettingsSection('Ansible');
        } else if (typeof window.openSettings === 'function') {
          window.openSettings();
        }
      });

    const search = document.getElementById('ansible-search');
    if (search) search.addEventListener('input', renderPlaybooks);

    document.getElementById('ansible-stop').addEventListener('click', stopRun);
    document.getElementById('ansible-report').addEventListener('click', copyReport);

    _initRunDialog();
    _initEditor();
    _initEventViewer();

    document.getElementById('ansible-new')
      .addEventListener('click', () => openEditor(''));

    // Escape closes the topmost thing, not everything. Three of these stack
    // over the panel, and one keypress taking the lot away reads as the
    // dialog having done something. shellmateDialog stops the event before
    // it reaches here, so its own Escape cannot close the panel behind it.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const stack = ['ansible-event-overlay', 'ansible-edit-overlay',
                     'ansible-run-overlay'];
      for (const id of stack) {
        const el = document.getElementById(id);
        if (el && !el.classList.contains('hidden')) {
          el.classList.add('hidden');
          if (id === 'ansible-edit-overlay') editing = '';
          return;
        }
      }
      if (!overlay.classList.contains('hidden')) closeAnsible();
    });

    renderHistory();
  });

  async function openAnsible() {
    overlay.classList.remove('hidden');
    await Promise.all([refreshStatus(), refreshPlaybooks()]);
    renderHistory();
  }

  function closeAnsible() {
    overlay.classList.add('hidden');
  }

  // -------------------------------------------------------------------------
  // The runner
  // -------------------------------------------------------------------------

  /**
   * Say what the runner is, honestly.
   *
   * `configured` and `reachable` are different answers and the panel states
   * each differently: "no runner set up yet" is a thing not done, and
   * "configured but not answering" is a thing gone wrong. Collapsing the two
   * into one red pill sends somebody checking a firewall for a certificate
   * they never chose.
   */
  async function refreshStatus() {
    const pill = document.getElementById('ansible-status-pill');
    const detail = document.getElementById('ansible-status-detail');
    if (!pill) return null;
    try {
      const data = await (await fetch('/api/ansible/status')).json();
      runnerReady = !!(data.configured && data.reachable);
      if (!data.configured) {
        _pill(pill, 'grey', 'Not set up');
        detail.textContent = data.detail
          ? `Still needed: ${data.detail}.`
          : 'No runner has been set up yet.';
      } else if (data.reachable) {
        _pill(pill, 'ok', 'Answering');
        detail.textContent = data.detail || 'The runner answered.';
      } else {
        _pill(pill, 'error', 'Not answering');
        detail.textContent = data.detail || 'The runner did not answer.';
      }
      return data;
    } catch (e) {
      runnerReady = false;
      _pill(pill, 'error', 'Not answering');
      detail.textContent = String(e.message || e);
      return null;
    }
  }

  function _pill(el, kind, text) {
    el.className = `ansible-pill ansible-pill-${kind}`;
    el.textContent = text;
  }

  // -------------------------------------------------------------------------
  // The two playbook lists
  // -------------------------------------------------------------------------

  let playbooks = { runner: [], library: [], error: '' };

  async function refreshPlaybooks() {
    try {
      playbooks = await (await fetch('/api/ansible/playbooks')).json();
    } catch (e) {
      playbooks = { runner: [], library: [], error: String(e.message || e) };
    }
    renderPlaybooks();
  }

  function renderPlaybooks() {
    const term = (document.getElementById('ansible-search').value || '')
      .trim().toLowerCase();
    const match = (name) => !term || name.toLowerCase().includes(term);

    // On the runner.
    const runnerList = document.getElementById('ansible-runner-list');
    runnerList.innerHTML = '';
    const names = (playbooks.runner || []).filter(match);
    if (playbooks.error) {
      // The runner's own words. "playbook file not found" is worth more than
      // "the runner answered 404", which is why the backend keeps them.
      const note = document.createElement('div');
      note.className = 'ansible-empty ansible-empty-error';
      note.textContent = `The runner did not answer: ${playbooks.error}`;
      runnerList.appendChild(note);
    } else if (!names.length) {
      runnerList.appendChild(_empty(
        term ? 'Nothing on the runner matches that.'
             : 'The runner holds no playbooks.'));
    }
    names.forEach(name => runnerList.appendChild(_runnerRow(name)));

    // In ShellMate.
    const libList = document.getElementById('ansible-library-list');
    libList.innerHTML = '';
    const mine = (playbooks.library || []).filter(f => match(f.name));
    if (!mine.length) {
      libList.appendChild(_empty(
        term ? 'Nothing here matches that.'
             : 'Nothing written here yet. New playbook starts one.'));
    }
    mine.forEach(f => libList.appendChild(_libraryRow(f)));
  }

  function _empty(text) {
    const el = document.createElement('div');
    el.className = 'ansible-empty';
    el.textContent = text;
    return el;
  }

  /**
   * One row.
   *
   * createElement throughout rather than interpolation: a playbook name is a
   * filename off a container's disk, and a quote in one must not be able to
   * rewrite the row it lands in.
   */
  function _row(icon, name, metaText, tagText, tagKind) {
    const row = document.createElement('div');
    row.className = 'ansible-row';

    const glyph = document.createElement('span');
    glyph.className = 'material-symbols-outlined ansible-row-icon';
    glyph.textContent = icon;

    const info = document.createElement('div');
    info.className = 'ansible-row-info';
    const title = document.createElement('span');
    title.className = 'ansible-row-name';
    title.textContent = name;
    const meta = document.createElement('span');
    meta.className = 'ansible-row-meta';
    meta.textContent = metaText;
    info.append(title, meta);

    const tag = document.createElement('span');
    tag.className = `ansible-tag ansible-tag-${tagKind}`;
    tag.textContent = tagText;

    row.append(glyph, info, tag);
    return row;
  }

  function _runnerRow(name) {
    const row = _row('automation', name, 'On the runner', 'Runner', 'runner');
    const run = _button('Run', 'bolt', 'btn-secondary');
    run.addEventListener('click', () => openRunDialog(name, 'runner'));
    row.appendChild(run);
    return row;
  }

  function _libraryRow(file) {
    const when = file.modified
      ? new Date(file.modified * 1000).toLocaleString() : '';
    const row = _row('code', file.name,
                     `${when} · ${(file.bytes / 1024).toFixed(1)} KB`,
                     'ShellMate', 'library');

    const edit = _button('Edit', 'edit', 'btn-secondary');
    edit.addEventListener('click', () => openEditor(file.name));
    row.appendChild(edit);

    // Runnable only once the runner holds a file by that name — the service
    // runs what is in its project directory, not what is in ours. Offering
    // Run on something the runner has never seen produces a 502 with the
    // runner saying "not found", three clicks after the useful moment.
    if ((playbooks.runner || []).includes(file.name)) {
      const run = _button('Run', 'bolt', 'btn-secondary');
      run.addEventListener('click', () => openRunDialog(file.name, 'library'));
      row.appendChild(run);
    }
    return row;
  }

  function _button(label, icon, cls) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `${cls} ansible-row-btn`;
    const glyph = document.createElement('span');
    glyph.className = 'material-symbols-outlined';
    glyph.textContent = icon;
    btn.append(glyph, document.createTextNode(label));
    return btn;
  }

  // -------------------------------------------------------------------------
  // The Run dialog
  // -------------------------------------------------------------------------

  function _initRunDialog() {
    const dialog = document.getElementById('ansible-run-overlay');

    document.getElementById('ansible-run-close')
      .addEventListener('click', closeRunDialog);
    document.getElementById('ansible-run-cancel')
      .addEventListener('click', closeRunDialog);
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) closeRunDialog();
    });

    document.querySelectorAll('input[name="ansible-target"]').forEach(radio => {
      radio.addEventListener('change', _showTargetPane);
    });

    document.getElementById('ansible-group-select')
      .addEventListener('change', _describeEstateTarget);

    document.getElementById('ansible-push-inventory')
      .addEventListener('click', pushInventory);

    document.getElementById('ansible-run-go').addEventListener('click', startRun);
  }

  async function openRunDialog(name, source) {
    running = { name, source };
    document.getElementById('ansible-run-name').textContent = name;
    document.getElementById('ansible-run-error').classList.add('hidden');
    document.getElementById('ansible-run-summary').textContent = '';
    // Ticked every time it opens, not merely on the first. A dry run is the
    // safe default and it must not be quietly inherited from whatever the
    // last run happened to be.
    document.getElementById('ansible-run-check').checked = true;
    document.getElementById('ansible-run-overlay').classList.remove('hidden');

    // Only ask the runner for its own inventory when there is a runner to
    // ask. Firing the call regardless answers 409, logs a console error, and
    // leaves a radio offering a list that cannot arrive — a choice that is
    // there but does not work is worse than one that says why it is not.
    await Promise.all(runnerReady
      ? [_loadEstate(), _loadRunnerInventory()]
      : [_loadEstate(), _noRunnerInventory()]);
    _showTargetPane();
  }

  function closeRunDialog() {
    document.getElementById('ansible-run-overlay').classList.add('hidden');
  }

  function _showTargetPane() {
    const mode = _targetMode();
    document.getElementById('ansible-target-group')
      .classList.toggle('hidden', mode !== 'group');
    document.getElementById('ansible-target-hosts')
      .classList.toggle('hidden', mode !== 'hosts');
    document.getElementById('ansible-target-runner')
      .classList.toggle('hidden', mode !== 'runner');
  }

  function _targetMode() {
    const chosen = document.querySelector('input[name="ansible-target"]:checked');
    return chosen ? chosen.value : 'group';
  }

  /** ShellMate's own estate, shaped as an inventory. Sends nothing anywhere. */
  async function _loadEstate() {
    try {
      estate = await (await fetch('/api/ansible/inventory')).json();
    } catch (_) {
      estate = { groups: {}, hosts: [], hostvars: {}, skipped: [] };
    }

    const select = document.getElementById('ansible-group-select');
    select.innerHTML = '';
    const all = document.createElement('option');
    all.value = '';
    all.textContent = `Every connection (${(estate.hosts || []).length})`;
    select.appendChild(all);
    Object.keys(estate.groups || {}).sort().forEach(key => {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = `${key} (${estate.groups[key].length})`;
      select.appendChild(option);
    });

    const hosts = document.getElementById('ansible-host-list');
    hosts.innerHTML = '';
    (estate.hosts || []).forEach(address => {
      const vars = (estate.hostvars || {})[address] || {};
      const label = document.createElement('label');
      label.className = 'ansible-host';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = address;
      const text = document.createElement('span');
      // The connection's name reads better than an address, but the address
      // is what Ansible connects to — so both, name first.
      text.textContent = vars.shellmate_name && vars.shellmate_name !== address
        ? `${vars.shellmate_name} — ${address}` : address;
      label.append(box, text);
      hosts.appendChild(label);
    });
    if (!(estate.hosts || []).length) {
      hosts.appendChild(_empty('No saved SSH connections to run against.'));
    }

    _describeEstateTarget();
  }

  /**
   * What the chosen group covers, and what it leaves out.
   *
   * A serial connection has no address for Ansible to reach, so the backend
   * drops it with its reason. Saying so here is the point: somebody
   * otherwise goes hunting for a device that never ran.
   */
  function _describeEstateTarget() {
    const host = document.getElementById('ansible-estate-preview');
    host.innerHTML = '';
    const group = document.getElementById('ansible-group-select').value;
    const hosts = group ? ((estate.groups || {})[group] || []) : (estate.hosts || []);

    const line = document.createElement('div');
    line.className = 'ansible-preview-line';
    line.textContent = hosts.length
      ? `${hosts.length} host${hosts.length === 1 ? '' : 's'}: ${hosts.slice(0, 8).join(', ')}`
        + (hosts.length > 8 ? `, and ${hosts.length - 8} more` : '')
      : 'No hosts in that group.';
    host.appendChild(line);

    (estate.skipped || []).forEach(entry => {
      const note = document.createElement('div');
      note.className = 'ansible-preview-skip';
      note.textContent = `Left out — ${entry.name}: ${entry.why}`;
      host.appendChild(note);
    });
  }

  /** No runner to ask, so the choice that depends on one is closed off. */
  function _noRunnerInventory() {
    const radio = document.querySelector('input[name="ansible-target"][value="runner"]');
    if (radio) {
      radio.disabled = true;
      // If it was the chosen one, fall back rather than leaving the dialog on
      // a pane with nothing in it.
      if (radio.checked) {
        const group = document.querySelector('input[name="ansible-target"][value="group"]');
        if (group) group.checked = true;
      }
    }
    const select = document.getElementById('ansible-runner-group');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'The runner is not answering';
    select.appendChild(option);
    return Promise.resolve();
  }

  /** The groups the container already holds. */
  /**
   * What the runner would use if ShellMate sends no inventory (#585).
   *
   * The service has no endpoint listing groups or hosts — its inventory is
   * a directory it holds, with group_vars and host_vars beside it that
   * merge automatically. So there is nothing to enumerate: the choice is
   * its own default, or a path somebody names.
   */
  async function _loadRunnerInventory() {
    const radio = document.querySelector('input[name="ansible-target"][value="runner"]');
    if (radio) radio.disabled = false;
    const select = document.getElementById('ansible-runner-group');
    if (!select) return;
    select.innerHTML = '';
    const own = document.createElement('option');
    own.value = '';
    own.textContent = "the runner's own inventory";
    select.appendChild(own);
  }

  /**
   * Show what a run against the estate would be pointed at (#585).
   *
   * Nothing is sent by looking, and nothing is pushed at all any more: the
   * inventory travels with the run itself, so the runner keeps none of
   * ShellMate's state. This is the last chance to see which hosts are in
   * it — and which were left out, and why — before anything starts.
   */
  async function pushInventory() {
    const group = document.getElementById('ansible-group-select').value;
    const button = document.getElementById('ansible-push-inventory');
    if (button) button.disabled = true;
    try {
      const data = await _json('/api/ansible/inventory'
        + (group ? `?group=${encodeURIComponent(group)}` : ''));
      const left = (data.skipped || []).map(x => `${x.name} — ${x.why}`).join('\n');
      await window.shellmateDialog.alert({
        title: `${(data.hosts || []).length} host(s) in this inventory`,
        body: (left ? `Left out:\n${left}\n\n` : '')
              + 'This travels with the run. The runner keeps none of it.',
      });
    } catch (e) {
      await window.shellmateDialog.alert({ title: 'Could not build the inventory',
                                           body: String(e.message || e) });
    } finally {
      if (button) button.disabled = false;
    }
  }

  /**
   * Extra vars, as somebody would actually type them.
   *
   * `name=value` lines are what a person reaches for; JSON is what the API
   * takes. Both are accepted and a malformed one is refused here, because the
   * alternative is the runner refusing the whole play over a stray brace.
   */
  function _parseExtraVars(text) {
    const body = (text || '').trim();
    if (!body) return {};
    if (body.startsWith('{')) {
      const parsed = JSON.parse(body);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Extra variables must be a JSON object.');
      }
      return parsed;
    }
    const out = {};
    body.split(/\r?\n/).forEach(line => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) return;
      const cut = trimmed.indexOf('=');
      if (cut < 1) throw new Error(`Not a name=value line: ${trimmed}`);
      out[trimmed.slice(0, cut).trim()] = trimmed.slice(cut + 1).trim();
    });
    return out;
  }

  function _chosenLimit() {
    const mode = _targetMode();
    if (mode === 'runner') {
      const group = document.getElementById('ansible-runner-group').value;
      // A group name is a limit the runner understands, and using it keeps
      // the runner's own inventory the authority on what is in it.
      return group ? [group] : [];
    }
    if (mode === 'hosts') {
      return [...document.querySelectorAll('#ansible-host-list input:checked')]
        .map(box => box.value);
    }
    const group = document.getElementById('ansible-group-select').value;
    return group ? ((estate.groups || {})[group] || []) : (estate.hosts || []);
  }

  async function startRun() {
    const error = document.getElementById('ansible-run-error');
    error.classList.add('hidden');

    let extraVars;
    try {
      extraVars = _parseExtraVars(document.getElementById('ansible-extra-vars').value);
    } catch (e) {
      error.textContent = String(e.message || e);
      error.classList.remove('hidden');
      return;
    }

    const limit = _chosenLimit();
    if (!limit.length) {
      error.textContent = 'Nothing is selected to run against.';
      error.classList.remove('hidden');
      return;
    }

    const check = document.getElementById('ansible-run-check').checked;
    const tags = document.getElementById('ansible-tags').value.trim();

    // Real changes are confirmed; a dry run is not. Check mode is the whole
    // reason there is a difference worth confirming.
    if (!check && window.shellmateDialog) {
      const go = await window.shellmateDialog.confirm({
        title: 'Run for real?',
        body: `${running.name} will run against ${limit.length} target`
              + `${limit.length === 1 ? '' : 's'} with check mode off, so it `
              + 'will make changes.',
        note: 'Ticking Check mode reports what would change without changing it.',
        confirmLabel: 'Run it',
        destructive: true,
      });
      if (!go) return;
    }

    const go = document.getElementById('ansible-run-go');
    go.disabled = true;
    try {
      const res = await fetch('/api/ansible/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          playbook: running.name, extra_vars: extraVars, limit, tags, check,
          // Where the hosts come from (#585): an estate inventory is built
          // here and travels with the run; the runner's own is a path.
          inventory_source: _targetMode() === 'runner' ? 'runner' : 'estate',
          group: _targetMode() === 'group'
            ? ((document.getElementById('ansible-group-select') || {}).value || '') : '',
          inventory_path: _targetMode() === 'runner'
            ? ((document.getElementById('ansible-runner-group') || {}).value || '') : '',
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        // 409 is "no runner configured", 502 is "the runner refused" — and
        // `detail` is the runner's own words in both cases, so it is shown
        // verbatim rather than rewritten into something friendlier and less
        // true.
        throw new Error(data.detail || `the server answered ${res.status}`);
      }
      closeRunDialog();
      watch({ uuid: data.id, playbook: running.name,
              target: _targetDescription(limit), check });
    } catch (e) {
      error.textContent = String(e.message || e);
      error.classList.remove('hidden');
    } finally {
      go.disabled = false;
    }
  }

  function _targetDescription(limit) {
    const mode = _targetMode();
    if (mode === 'runner') return `the runner's ${limit[0] || 'inventory'}`;
    if (mode === 'group') {
      const group = document.getElementById('ansible-group-select').value;
      return group ? `${group} (${limit.length})` : `every connection (${limit.length})`;
    }
    return `${limit.length} host${limit.length === 1 ? '' : 's'}`;
  }

  // -------------------------------------------------------------------------
  // Watching a run
  // -------------------------------------------------------------------------

  function watch(run) {
    stopPolling();
    live = { ...run, since: '' };
    seenEvents = [];
    lastState = null;

    document.getElementById('ansible-run-section').classList.remove('hidden');
    document.getElementById('ansible-live-name').textContent = run.playbook;
    document.getElementById('ansible-live-target').textContent =
      `against ${run.target}${run.check ? ' · check mode' : ''}`;
    document.getElementById('ansible-events').innerHTML = '';
    document.getElementById('ansible-stop').classList.remove('hidden');
    document.getElementById('ansible-report').classList.add('hidden');
    _pill(document.getElementById('ansible-live-pill'), 'live', 'Starting');

    remember(run);
    poll();
  }

  function stopPolling() {
    if (live && live.timer) clearTimeout(live.timer);
  }

  /**
   * One round: what the run is doing, then whatever is new since last time.
   *
   * Status first, because a finished run still has events to collect and
   * asking in the other order can end the poll one round before the last
   * task arrives.
   */
  async function poll() {
    if (!live) return;
    const uuid = live.uuid;
    try {
      // res.ok, not just the body. A 409 (no runner) and a 502 (the runner
      // refused) both answer with `{detail: …}`, and reading one of those as
      // a state object leaves the panel showing an empty run for ever
      // instead of saying what went wrong.
      const state = await _json(`/api/ansible/jobs/${encodeURIComponent(uuid)}`);
      if (!live || live.uuid !== uuid) return;
      lastState = state;
      _renderState(state);

      const fresh = await _json(
        `/api/ansible/jobs/${encodeURIComponent(uuid)}/events`
        + `?since=${encodeURIComponent(live.since)}`);
      if (!live || live.uuid !== uuid) return;
      (fresh.events || []).forEach(appendEvent);
      if (fresh.events && fresh.events.length) {
        live.since = fresh.last || fresh.events[fresh.events.length - 1].counter;
      }

      if (state.finished) {
        finish(state);
        return;
      }
    } catch (e) {
      _pill(document.getElementById('ansible-live-pill'), 'error', 'Lost the run');
      document.getElementById('ansible-live-target').textContent =
        String(e.message || e);
      return;
    }
    live.timer = setTimeout(poll, POLL_MS);
  }

  /**
   * A GET whose failure is the server's own words.
   *
   * `detail` carries the runner's message through both error codes the
   * Ansible endpoints raise — 409 for no runner configured, 502 for a runner
   * that refused — and it is far more use than the status number.
   */
  async function _json(url) {
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
    return data;
  }

  function _renderState(state) {
    const pill = document.getElementById('ansible-live-pill');
    const label = state.status || (state.running ? 'running' : 'unknown');
    if (state.running) _pill(pill, 'live', label);
    else if (label === 'successful') _pill(pill, 'ok', 'Successful');
    else if (label) _pill(pill, 'error', label);
    _renderTallies(state.summary || {});
  }

  /** The per-host tallies, in the words Ansible itself uses. */
  function _renderTallies(summary) {
    const host = document.getElementById('ansible-tallies');
    host.innerHTML = '';
    [['tasks', 'tasks', 'neutral'], ['ok', 'ok', 'ok'],
     ['changed', 'changed', 'changed'], ['failed', 'failed', 'error'],
     ['unreachable', 'unreachable', 'error'], ['skipped', 'skipped', 'neutral'],
     ['other', 'other', 'neutral']].forEach(([key, label, kind]) => {
      const count = summary[key] || 0;
      if (!count && kind === 'neutral' && key !== 'tasks') return;
      const chip = document.createElement('span');
      chip.className = `ansible-tally ansible-tally-${kind}`;
      const value = document.createElement('strong');
      value.textContent = String(count);
      chip.append(value, document.createTextNode(` ${label}`));
      host.appendChild(chip);
    });
  }

  /** The event names worth a row of their own, and how each one reads. */
  const EVENT_KINDS = {
    playbook_on_task_start: ['task',    'neutral'],
    playbook_on_play_start: ['play',    'neutral'],
    runner_on_ok:           ['ok',      'ok'],
    runner_on_changed:      ['changed', 'changed'],
    runner_on_failed:       ['failed',  'error'],
    runner_on_async_failed: ['failed',  'error'],
    runner_on_unreachable:  ['unreachable', 'error'],
    runner_on_skipped:      ['skipped', 'neutral'],
  };

  function appendEvent(entry) {
    seenEvents.push(entry);
    const known = EVENT_KINDS[entry.event];
    if (!known) return;               // stats and internals are not rows
    const [label, kind] = known;

    const host = document.getElementById('ansible-events');
    const row = document.createElement('div');
    row.className = `ansible-event ansible-event-${kind}`;

    const badge = document.createElement('span');
    badge.className = `ansible-tag ansible-tag-${kind}`;
    badge.textContent = label;

    const text = document.createElement('span');
    text.className = 'ansible-event-text';
    text.textContent = entry.task || entry.event_data_host || entry.event || '';

    const who = document.createElement('span');
    who.className = 'ansible-event-host';
    who.textContent = entry.host || entry.event_data_host || '';

    row.append(badge, text, who);
    // Every task's own output is one call away, and it is where a failure
    // explains itself — so the whole row opens it rather than a small button
    // somebody has to notice.
    row.title = 'Click to see what this task said';
    row.addEventListener('click', () => openEvent(entry));

    // Stuck to the bottom only while the reader is already there: yanking the
    // list down while somebody is reading an earlier failure is worse than a
    // list that needs a scroll.
    const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 40;
    host.appendChild(row);
    if (atBottom) host.scrollTop = host.scrollHeight;
  }

  function finish(state) {
    stopPolling();
    document.getElementById('ansible-stop').classList.add('hidden');
    document.getElementById('ansible-report').classList.remove('hidden');
    if (live) {
      remember({ ...live, status: state.status, summary: state.summary,
                 finished: true });
      live.timer = null;
    }
    renderHistory();
  }

  async function stopRun() {
    if (!live) return;
    const uuid = live.uuid;
    const button = document.getElementById('ansible-stop');
    button.disabled = true;
    try {
      const res = await fetch(`/api/ansible/jobs/${encodeURIComponent(uuid)}`,
                              { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
      // "That run had already finished" is a normal outcome — somebody
      // reached for Stop as the last task landed — so it is reported as what
      // happened rather than as a failure.
      _notify(data.cancelled ? 'info' : 'warning',
              data.cancelled ? 'Stopping' : 'Nothing to stop',
              data.detail || '');
    } catch (e) {
      _notify('warning', 'Could not stop the run', String(e.message || e));
    } finally {
      button.disabled = false;
    }
  }

  // -------------------------------------------------------------------------
  // One task's output
  // -------------------------------------------------------------------------

  let _eventText = '';

  function _initEventViewer() {
    const dialog = document.getElementById('ansible-event-overlay');
    document.getElementById('ansible-event-close')
      .addEventListener('click', () => dialog.classList.add('hidden'));
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) dialog.classList.add('hidden');
    });
    document.getElementById('ansible-event-copy')
      .addEventListener('click', () => _copy(_eventText));
  }

  async function openEvent(entry) {
    const dialog = document.getElementById('ansible-event-overlay');
    const content = document.getElementById('ansible-event-content');
    document.getElementById('ansible-event-title').textContent =
      entry.task || entry.event || 'Task';
    document.getElementById('ansible-event-meta').textContent =
      [entry.host || '', entry.event || ''].filter(Boolean).join(' · ');
    content.textContent = 'Loading…';
    dialog.classList.remove('hidden');

    if (!live) { content.textContent = 'No run to ask about.'; return; }
    try {
      const res = await fetch(
        `/api/ansible/jobs/${encodeURIComponent(live.uuid)}`
        + `/events/${encodeURIComponent(entry.event_id)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
      _eventText = JSON.stringify(data, null, 2);
      content.textContent = _eventText;
    } catch (e) {
      _eventText = '';
      content.textContent = `Could not read that task: ${e.message || e}`;
    }
  }

  // -------------------------------------------------------------------------
  // What this browser has watched
  // -------------------------------------------------------------------------

  const HISTORY_KEY = 'shellmate.ansible.runs';
  const HISTORY_MAX = 25;

  /**
   * Note a run down.
   *
   * The service has no endpoint that lists past runs, so this is ShellMate's
   * own note rather than the runner's record — and the panel says exactly
   * that, because a history that silently omits a colleague's run is worse
   * than one that admits its scope.
   */
  function remember(run) {
    let all = _history();
    all = all.filter(r => r.uuid !== run.uuid);
    all.unshift({ uuid: run.uuid, playbook: run.playbook, target: run.target,
                  check: !!run.check, status: run.status || 'running',
                  summary: run.summary || null,
                  at: run.at || Date.now() });
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(all.slice(0, HISTORY_MAX)));
    } catch (_) { /* a full or blocked store is not worth failing a run over */ }
  }

  function _history() {
    try {
      const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
      return Array.isArray(raw) ? raw : [];
    } catch (_) { return []; }
  }

  function renderHistory() {
    const host = document.getElementById('ansible-history');
    if (!host) return;
    host.innerHTML = '';
    const all = _history();
    if (!all.length) {
      host.appendChild(_empty('No runs watched from this browser yet.'));
      return;
    }
    all.forEach(run => {
      const summary = run.summary || {};
      const meta = [new Date(run.at).toLocaleString(),
                    `against ${run.target}`,
                    run.check ? 'check mode' : 'for real'].join(' · ');
      const kind = run.status === 'successful' ? 'ok'
        : (run.status === 'running' ? 'live' : 'error');
      const row = _row('history', run.playbook, meta, run.status || 'unknown', kind);

      if (summary.changed || summary.failed) {
        const counts = document.createElement('span');
        counts.className = 'ansible-row-counts';
        counts.textContent = `${summary.changed || 0} changed · ${summary.failed || 0} failed`;
        row.appendChild(counts);
      }

      const again = _button('Watch', 'visibility', 'btn-secondary');
      again.addEventListener('click', () => watch({
        uuid: run.uuid, playbook: run.playbook, target: run.target,
        check: run.check }));
      row.appendChild(again);
      host.appendChild(row);
    });
  }

  // -------------------------------------------------------------------------
  // The report
  // -------------------------------------------------------------------------

  /**
   * What ran, against what, what changed and what failed — as Markdown.
   *
   * It goes to the clipboard rather than to a file. ShellMate has no
   * Markdown report path to save through: `/api/logs/{name}/save` and
   * `/api/scrollback/save` both write a `.log` of terminal output through
   * the platform's folder dialog, and neither is a place a run report
   * belongs. A browser download is worse than useless here — in the native
   * window an `<a download>` quietly does nothing, which is the defect
   * logs.js already carries a note about. So: the clipboard, which works in
   * every frame ShellMate runs in, and lands straight in the change record
   * or the ticket this is going into anyway.
   */
  function buildReport() {
    const summary = (lastState && lastState.summary) || {};
    const status = (lastState && lastState.status) || 'unknown';
    const run = live || {};

    const failures = seenEvents.filter(
      e => e.event === 'runner_on_failed' || e.event === 'runner_on_async_failed'
           || e.event === 'runner_on_unreachable');
    const changes = seenEvents.filter(e => e.event === 'runner_on_changed');

    const lines = [
      `# Ansible run — ${run.playbook || 'a playbook'}`,
      '',
      `- **Playbook**: ${run.playbook || '(unknown)'}`,
      `- **Ran against**: ${run.target || '(unknown)'}`,
      // Said in words, not as a flag. "check mode" reads as a note; "no
      // changes were made" is the sentence somebody needs in a change record.
      `- **Mode**: ${run.check ? 'check mode — no changes were made'
                               : 'live — changes were made'}`,
      `- **Finished as**: ${status}`,
      `- **Run id**: ${run.uuid || '(unknown)'}`,
      `- **Reported**: ${new Date().toLocaleString()}`,
      '',
      '## Tallies',
      '',
      '| tasks | ok | changed | failed | unreachable | skipped |',
      '| --- | --- | --- | --- | --- | --- |',
      `| ${summary.tasks || 0} | ${summary.ok || 0} | ${summary.changed || 0} `
      + `| ${summary.failed || 0} | ${summary.unreachable || 0} | ${summary.skipped || 0} |`,
      '',
    ];

    lines.push('## What changed', '');
    if (changes.length) {
      changes.forEach(e => lines.push(
        `- ${e.host || 'a host'} — ${e.task || e.event}`));
    } else {
      lines.push(run.check
        ? 'Nothing. Check mode reports what would change and changes nothing.'
        : 'Nothing reported as changed.');
    }
    lines.push('');

    lines.push('## What failed', '');
    if (failures.length) {
      failures.forEach(e => lines.push(
        `- ${e.host || 'a host'} — ${e.task || e.event} (${e.event})`));
    } else {
      lines.push('Nothing.');
    }
    lines.push('');

    // The tasks in order, so the report says what ran and not only what went
    // wrong — a play that did nothing and a play that did twelve things
    // successfully otherwise read identically.
    const tasks = seenEvents.filter(e => e.event === 'playbook_on_task_start');
    if (tasks.length) {
      lines.push('## Tasks, in order', '');
      tasks.forEach((e, i) => lines.push(`${i + 1}. ${e.task || '(unnamed)'}`));
      lines.push('');
    }

    return lines.join('\n');
  }

  async function copyReport() {
    await _copy(buildReport());
    _notify('info', 'Report copied',
            'Markdown, on the clipboard — paste it into the change record.');
  }

  // -------------------------------------------------------------------------
  // The library editor
  // -------------------------------------------------------------------------

  /** The name the open playbook was loaded under; "" for one not saved yet. */
  let editing = '';

  /**
   * What a new playbook starts as.
   *
   * Not empty. A playbook is a list of plays and the backend refuses
   * anything that is not, so an empty box is a Save that fails on the first
   * press — and the shape is the part somebody writing their first one is
   * least sure of.
   */
  const SKELETON = [
    '---',
    '- name: Describe what this play is for',
    '  hosts: all',
    '  gather_facts: false',
    '  tasks:',
    '    - name: Describe the task',
    '      ansible.builtin.debug:',
    '        msg: "Replace me"',
    '',
  ].join('\n');

  function _initEditor() {
    const dialog = document.getElementById('ansible-edit-overlay');

    document.getElementById('ansible-edit-close')
      .addEventListener('click', closeEditor);
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) closeEditor();
    });

    document.getElementById('ansible-edit-save').addEventListener('click', savePlaybook);
    document.getElementById('ansible-edit-delete').addEventListener('click', deletePlaybook);
    document.getElementById('ansible-edit-send').addEventListener('click', sendPlaybook);
  }

  async function openEditor(name) {
    editing = name || '';
    const dialog = document.getElementById('ansible-edit-overlay');
    const nameField = document.getElementById('ansible-edit-name');
    const text = document.getElementById('ansible-edit-text');

    document.getElementById('ansible-edit-title').textContent =
      editing ? 'Playbook' : 'New playbook';
    nameField.value = editing;
    // Renaming an existing playbook would write a second file and leave the
    // first, which is a copy dressed up as a rename. The name is fixed once
    // the file exists; Delete is how one goes away.
    nameField.readOnly = !!editing;
    // Only for something that exists. Both act on a file on disk.
    document.getElementById('ansible-edit-delete').classList.toggle('hidden', !editing);
    document.getElementById('ansible-edit-send').classList.toggle('hidden', !editing);
    text.value = editing ? 'Loading\u2026' : SKELETON;
    _note('');
    dialog.classList.remove('hidden');

    if (!editing) { text.focus(); return; }
    try {
      const res = await fetch(`/api/ansible/library/${encodeURIComponent(editing)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
      text.value = data.text || '';
      _describeTransfer(data.transfer);
    } catch (e) {
      text.value = '';
      _note(`Could not open it: ${e.message || e}`, true);
    }
  }

  function closeEditor() {
    document.getElementById('ansible-edit-overlay').classList.add('hidden');
    editing = '';
  }

  function _note(text, bad) {
    const el = document.getElementById('ansible-edit-note');
    el.textContent = text;
    el.classList.toggle('ansible-note-bad', !!bad);
  }

  /** Where this playbook would land on the runner, and why it goes that way. */
  function _describeTransfer(transfer) {
    if (!transfer) { _note(''); return; }
    _note(`${transfer.why} It would land at ${transfer.target}.`);
  }

  /**
   * Save it, and show the parser's own words when it will not parse.
   *
   * The backend refuses YAML that does not load, and returns the message the
   * parser produced — "line 14, column 3" and all. Rewriting that into
   * "invalid playbook" would throw away the only part of it that helps.
   */
  async function savePlaybook() {
    const name = (document.getElementById('ansible-edit-name').value || '').trim();
    if (!name) { _note('Give it a name first.', true); return; }

    const button = document.getElementById('ansible-edit-save');
    button.disabled = true;
    try {
      const res = await fetch(`/api/ansible/library/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: document.getElementById('ansible-edit-text').value }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
      // The saved name, not the typed one: the backend adds .yml to a name
      // without a suffix, and the editor has to agree with the disk about
      // what this file is called or Send goes looking for the wrong one.
      editing = data.name;
      document.getElementById('ansible-edit-title').textContent = 'Playbook';
      document.getElementById('ansible-edit-name').value = data.name;
      document.getElementById('ansible-edit-name').readOnly = true;
      document.getElementById('ansible-edit-delete').classList.remove('hidden');
      document.getElementById('ansible-edit-send').classList.remove('hidden');
      _describeTransfer(data.transfer);
      await refreshPlaybooks();
    } catch (e) {
      _note(String(e.message || e), true);
    } finally {
      button.disabled = false;
    }
  }

  async function deletePlaybook() {
    if (!editing) return;
    if (window.shellmateDialog) {
      const go = await window.shellmateDialog.confirm({
        title: 'Delete this playbook?',
        body: `${editing} will be removed from ShellMate's library.`,
        note: 'A copy already sent to the runner stays where it is.',
        confirmLabel: 'Delete',
        destructive: true,
      });
      if (!go) return;
    }
    try {
      const res = await fetch(`/api/ansible/library/${encodeURIComponent(editing)}`,
                              { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
      closeEditor();
      await refreshPlaybooks();
    } catch (e) {
      _note(String(e.message || e), true);
    }
  }

  /**
   * Copy it to the runner over an SSH session.
   *
   * Which session has to be asked, not guessed: the container runs on one
   * machine and ShellMate may have a dozen tabs open to switches. Guessing
   * writes a playbook onto a 2960's flash.
   */
  async function sendPlaybook() {
    if (!editing) return;
    let sessions = [];
    try {
      const all = await (await fetch('/api/sessions')).json();
      sessions = (Array.isArray(all) ? all : (all.sessions || []))
        .filter(s => (s.connection_type || '').toLowerCase() === 'ssh');
    } catch (_) { /* handled by the empty case below */ }

    if (!sessions.length) {
      _note('No open SSH session to copy it over. Connect to the machine '
            + 'running the container first.', true);
      return;
    }

    let chosen = sessions[0].session_id;
    if (window.shellmateDialog) {
      const answer = await window.shellmateDialog.form({
        title: 'Send to the runner',
        body: 'The service has no upload API, so this copies the playbook over '
              + 'an SSH session you already have. Choose the session on the '
              + 'machine running the container.',
        fields: [{
          name: 'session', label: 'Over this session', type: 'select',
          options: sessions.map(s => ({
            value: s.session_id,
            label: `${s.display_label || s.hostname} (${s.target || s.hostname})`,
          })),
        }],
        confirmLabel: 'Send',
      });
      if (!answer) return;
      chosen = answer.session;
    }

    try {
      const res = await fetch(
        `/api/ansible/library/${encodeURIComponent(editing)}`
        + `/send/${encodeURIComponent(chosen)}`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
      // Where it landed, in full. "Sent" on its own leaves somebody guessing
      // which directory the runner reads, which is the whole question.
      _note(`Copied to ${data.target}.`);
      _notify('info', 'Playbook sent', `${editing} is now at ${data.target}.`);
      await refreshPlaybooks();
    } catch (e) {
      _note(String(e.message || e), true);
    }
  }

  // -------------------------------------------------------------------------
  // Odds and ends
  // -------------------------------------------------------------------------

  function _notify(severity, title, body) {
    if (window.shellmateAlerts && window.shellmateAlerts.notify) {
      window.shellmateAlerts.notify({
        global: true, severity, icon: severity === 'warning' ? 'error' : 'automation',
        title, body });
    }
  }

  async function _copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      // The route older webviews need; the clipboard API is not always there.
      const box = document.createElement('textarea');
      box.value = text;
      document.body.appendChild(box);
      box.select();
      try { document.execCommand('copy'); } catch (_) { /* give up quietly */ }
      box.remove();
    }
    if (typeof window._showCopyToast === 'function') window._showCopyToast();
  }

  window.openAnsible = openAnsible;
  // Named so the browser tests can reach the seams without a container in
  // the loop: a runner is not something a test suite can stand up, but the
  // dialog's defaults and the library's round trip are worth asserting
  // whether or not one is there.
  window._ansible = { refreshPlaybooks, renderPlaybooks, watch, openEditor,
                      openRunDialog, buildReport, _parseExtraVars };
})();
