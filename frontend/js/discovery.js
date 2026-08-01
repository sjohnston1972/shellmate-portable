/**
 * discovery.js — The panel that finds devices on the network.
 *
 * Backed entirely by `backend/discovery.py`; nothing here decides anything.
 * The size limit, the concurrency, the timeouts and the overall deadline are
 * all enforced server-side, because a limit the browser applies is a limit
 * anybody with the API can skip.
 *
 * Two things this interface owes the user.
 *
 * **Honest progress.** A sweep of a sparse /24 spends most of its time waiting
 * for addresses that will never answer, so without a count and a current
 * address it is indistinguishable from a hang. It polls rather than opening a
 * WebSocket: results are cheap to re-fetch, a poll cannot leak a connection,
 * and the panel being closed mid-scan should not stop the scan.
 *
 * **A number before a large scan.** "Scan 10.0.0.0/22?" means nothing to
 * anybody. "That is 1,022 addresses" means quite a lot. The confirmation asks
 * with the count in it, from the same parser that will do the work.
 */
(function () {
  'use strict';

  /** Above this, ask before starting. A /24 is 254 and needs no ceremony. */
  const CONFIRM_ABOVE = 256;

  /** How often to ask the backend how a running scan is getting on. */
  const POLL_MS = 700;

  let targetsEl, portsEl, startBtn, cancelBtn, statusEl;
  let progressEl, barEl, progressTextEl;
  let resultsEl, resultsHintEl, saveRowEl, saveStatusEl;
  let subnetHintEl;

  let scanId = null;
  let poller = null;
  let lastResults = [];
  const chosen = new Set();

  document.addEventListener('DOMContentLoaded', () => {
    const overlay = document.getElementById('discovery-overlay');
    if (!overlay) return;

    targetsEl      = document.getElementById('discovery-targets');
    portsEl        = document.getElementById('discovery-ports');
    startBtn       = document.getElementById('discovery-start');
    cancelBtn      = document.getElementById('discovery-cancel');
    statusEl       = document.getElementById('discovery-status');
    progressEl     = document.getElementById('discovery-progress');
    barEl          = document.getElementById('discovery-bar-fill');
    progressTextEl = document.getElementById('discovery-progress-text');
    resultsEl      = document.getElementById('discovery-results');
    resultsHintEl  = document.getElementById('discovery-results-hint');
    saveRowEl      = document.getElementById('discovery-save-row');
    saveStatusEl   = document.getElementById('discovery-save-status');
    subnetHintEl   = document.getElementById('discovery-subnet-hint');

    document.getElementById('sidebar-link-discovery')
      .addEventListener('click', (e) => { e.preventDefault(); open(); });
    document.getElementById('discovery-close')
      .addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

    startBtn.addEventListener('click', start);
    cancelBtn.addEventListener('click', stop);
    document.getElementById('discovery-save').addEventListener('click', save);
    document.getElementById('discovery-select-all')
      .addEventListener('click', selectAll);

    // From the connection dialog. The dialog stays open behind it: somebody
    // who finds the address wants to carry on filling the form in, not start
    // again.
    const fromDialog = document.getElementById('field-find-devices');
    if (fromDialog) fromDialog.addEventListener('click', (e) => {
      e.preventDefault();
      open();
    });

    targetsEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); start(); }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });
  });

  async function open() {
    document.getElementById('discovery-overlay').classList.remove('hidden');

    // The local subnet as the default: it is the answer nearly every time,
    // and it is the one target that cannot accidentally reach somebody
    // else's network.
    if (!targetsEl.value) {
      try {
        const res = await fetch('/api/discovery/subnets');
        const data = await res.json();
        if (data.subnets && data.subnets.length) {
          targetsEl.value = data.subnets[0].cidr;
          subnetHintEl.textContent =
            `This machine is on ${data.subnets[0].address}. Up to `
            + `${data.max_hosts.toLocaleString()} addresses per scan.`;
        }
        if (!portsEl.value) portsEl.value = data.ports || '22,23,80,443';
      } catch (e) { /* a default is a convenience, not a requirement */ }
    }
    targetsEl.focus();
  }

  function close() {
    // Deliberately does not stop a running scan. Sessions live in the server
    // and so does this; closing the panel to look at a terminal must not
    // throw away a sweep that is half done.
    document.getElementById('discovery-overlay').classList.add('hidden');
  }

  async function start() {
    if (scanId) return;
    setStatus('');

    const targets = targetsEl.value.trim();
    if (!targets) { setStatus('Give something to scan.', true); return; }

    // Ask the backend what this comes to before committing to it — same
    // parser, so the number in the question is the number that will be
    // scanned.
    let count = 0;
    try {
      const res = await fetch('/api/discovery/preview', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ targets }),
      });
      const data = await res.json();
      if (!res.ok) { setStatus(data.detail || 'That is not a target ShellMate understands.', true); return; }
      count = data.count;
    } catch (e) {
      setStatus('Could not work out what that means.', true);
      return;
    }

    if (count > CONFIRM_ABOVE) {
      const ok = await confirmWith(
        `Scan ${count.toLocaleString()} addresses?`,
        'Each one is a real connection attempt, and the network will log '
        + 'them. This can take a while — you can stop it at any point.');
      if (!ok) return;
    }

    try {
      const res = await fetch('/api/discovery/scans', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ targets, ports: portsEl.value.trim() }),
      });
      const state = await res.json();
      if (!res.ok) { setStatus(state.detail || 'Could not start the scan.', true); return; }

      scanId = state.id;
      chosen.clear();
      running(true);
      render(state);
      poller = setInterval(poll, POLL_MS);
    } catch (e) {
      setStatus('Could not start the scan.', true);
    }
  }

  async function poll() {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/discovery/scans/${scanId}`);
      if (!res.ok) { finish(); return; }
      const state = await res.json();
      render(state);
      if (!state.running) finish(state);
    } catch (e) {
      // A poll that fails is not a scan that failed — the sweep is running in
      // the server either way. Keep asking.
    }
  }

  async function stop() {
    if (!scanId) return;
    cancelBtn.disabled = true;
    try {
      await fetch(`/api/discovery/scans/${scanId}/cancel`, { method: 'POST' });
    } finally {
      cancelBtn.disabled = false;
    }
  }

  function finish(state) {
    clearInterval(poller);
    poller = null;
    scanId = null;
    running(false);
    if (state && state.error) setStatus(state.error, true);
    else if (state && state.cancelled) setStatus(`Stopped after ${state.scanned} of ${state.total}.`);
    else if (state) setStatus(`Done — ${state.found} found in ${state.total} addresses, ${state.elapsed}s.`);
  }

  function running(active) {
    startBtn.disabled = active;
    cancelBtn.classList.toggle('hidden', !active);
    progressEl.classList.toggle('hidden', !active);
  }

  function render(state) {
    const done = state.total ? Math.round((state.scanned / state.total) * 100) : 0;
    barEl.style.width = `${done}%`;
    progressTextEl.textContent =
      `${state.scanned} of ${state.total} scanned · ${state.found} found`
      + (state.current ? ` · probing ${state.current}` : '');

    // Only redraw when the set has actually changed. A scan of a /24 polls
    // three hundred times, and rebuilding the list under a user's cursor each
    // time makes the tick boxes unusable.
    const signature = state.results.map(r => r.address).join(',');
    if (signature === lastResults.map(r => r.address).join(',')) return;
    lastResults = state.results;

    resultsHintEl.classList.toggle('hidden', state.results.length > 0);
    saveRowEl.classList.toggle('hidden', state.results.length === 0);

    resultsEl.innerHTML = '';
    state.results.forEach(device => resultsEl.appendChild(row(device)));
  }

  function row(device) {
    const el = document.createElement('label');
    el.className = 'discovery-row';

    const tick = document.createElement('input');
    tick.type = 'checkbox';
    tick.checked = chosen.has(device.address);
    tick.addEventListener('change', () => {
      if (tick.checked) chosen.add(device.address);
      else chosen.delete(device.address);
    });

    const detail = document.createElement('div');
    detail.className = 'discovery-detail';

    const line = document.createElement('span');
    line.className = 'discovery-address';
    // textContent throughout — every string below came off the network, from
    // a device nobody has authenticated to. A page title is attacker-supplied
    // text in the most literal sense available.
    line.textContent = device.address
      + (device.hostname ? `  ${device.hostname}` : '');

    const what = document.createElement('span');
    what.className = 'discovery-what';
    what.textContent = describe(device);

    detail.append(line, what);

    const ports = document.createElement('span');
    ports.className = 'discovery-ports';
    ports.textContent = device.ports.join(', ');

    el.append(tick, detail, ports);

    if (device.platform && device.platform !== 'generic') {
      const badge = document.createElement('span');
      badge.className = 'discovery-badge';
      badge.textContent = device.platform_name || device.platform;
      badge.title = `Identified from the SSH banner, confidence `
        + `${device.confidence}. Shown, not acted on.`;
      el.insertBefore(badge, ports);
    }

    return el;
  }

  /** One line saying what this thing appears to be. */
  function describe(device) {
    const parts = [];
    if (device.model) parts.push(device.model);
    if (device.version) parts.push(`version ${device.version}`);
    if (device.http && device.http.title) parts.push(`“${device.http.title}”`);
    else if (device.http && device.http.server) parts.push(device.http.server);
    if (device.certificate) parts.push(device.certificate);
    if (!parts.length && device.ssh_banner) parts.push(device.ssh_banner);
    return parts.join(' · ');
  }

  function selectAll() {
    const boxes = resultsEl.querySelectorAll('input[type="checkbox"]');
    const turningOn = chosen.size < lastResults.length;
    chosen.clear();
    boxes.forEach((box, index) => {
      box.checked = turningOn;
      if (turningOn) chosen.add(lastResults[index].address);
    });
  }

  async function save() {
    const devices = lastResults.filter(d => chosen.has(d.address));
    if (!devices.length) {
      saveStatusEl.textContent = 'Tick the ones worth keeping first.';
      return;
    }

    saveStatusEl.textContent = 'Saving…';
    try {
      const res = await fetch('/api/discovery/save', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ devices }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not save them.');

      // save_profile() refuses to create a second entry for a device it
      // already has, so a repeated scan says so rather than doubling the list.
      const bits = [];
      if (data.saved) bits.push(`${data.saved} saved`);
      if (data.already_saved) bits.push(`${data.already_saved} already known`);
      saveStatusEl.textContent = bits.join(', ') + '.';

      if (typeof window.renderWelcomeProfiles === 'function') {
        window.renderWelcomeProfiles();
      }
    } catch (e) {
      saveStatusEl.textContent = e.message;
    }
  }

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.classList.toggle('discovery-error', Boolean(isError));
  }

  function confirmWith(title, body) {
    if (window.shellmateDialog) {
      return window.shellmateDialog.confirm({ title, body, confirmLabel: 'Scan' });
    }
    return Promise.resolve(window.confirm(`${title}\n\n${body}`));
  }

  /** Opened from the connection dialog, where somebody realises they do not
   *  know the address. */
  window.shellmateDiscovery = { open };
})();
