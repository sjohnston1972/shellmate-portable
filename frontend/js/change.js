/**
 * change.js — Bracketing a piece of work (#544).
 *
 * Two menu entries and a record. "Start a change…" captures the
 * configuration and pins it; "End change" captures again, diffs against the
 * pin, and shows what was typed in between.
 *
 * **The record is drawn by the diff window, not by a panel of its own.**
 * What a change record is, mostly, is a diff — and there is already a
 * window that renders one, with hunks, a capture history, Explain, Copy
 * all, Export and "Propose the way back". A second window would be a
 * second implementation of all of that, and the two would come to disagree
 * about how they present the same hunks. So the end report is deliberately
 * shaped like the drift report, and drift.js grows one block for the parts
 * a change has that drift does not: the note, the window, the commands, and
 * anything still pending.
 *
 * **Which devices have a change open is cached here.** The menu is built
 * synchronously and the answer lives on the server, so the alternative is
 * either a menu that waits on a round trip or one that offers both entries
 * always. The cache is refreshed on load, whenever the set of sessions
 * moves, and after every start and end. When it is wrong the menu offers
 * the wrong
 * one of two entries and the server refuses with a message that says
 * exactly what is open — which is why the 409 and the 404 on those routes
 * are worded the way they are.
 */
(function () {
  'use strict';

  /** Hostnames, case-folded, with a change window open. */
  let openHosts = new Set();

  function key(name) {
    return String(name || '').trim().toLowerCase();
  }

  /** What a change on this tab would be about — the server's own rule. */
  function deviceOf(tab) {
    if (!tab) return '';
    return String(tab.hostname || tab.label || tab.target || '').trim();
  }

  async function refresh() {
    try {
      const res = await fetch('/api/changes');
      if (!res.ok) return;
      const data = await res.json();
      openHosts = new Set((data.changes || []).map(c => key(c.hostname)));
    } catch (_) {
      // Leave the cache as it was. A failed refresh must not silently
      // report every change as closed, which would offer "Start a change"
      // on a device already inside one.
    }
  }

  function hasOpen(tab) {
    return openHosts.has(key(deviceOf(tab)));
  }

  function notify(spec) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({ global: true, ...spec });
    }
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  }

  /**
   * Open a window on this tab's device.
   *
   * The capture happens server-side and can take several seconds on a big
   * configuration, so the toast comes after it rather than before — an
   * "opened" message followed by a failure would be two contradictory
   * things on screen.
   */
  async function start(tab) {
    if (!tab || !tab.sessionId) return;
    const device = deviceOf(tab) || 'this device';

    const answer = await window.shellmateDialog.form({
      title: `Start a change on ${device}`,
      body: 'The configuration is captured now and pinned, so that ending '
          + 'the change compares against this moment rather than against '
          + 'whenever you last logged in. What you type in between is '
          + 'gathered from the session history.',
      fields: [
        { name: 'note', label: 'What is this change for?',
          placeholder: 'Replacing the uplink SFP on Gi1/0/49' },
        { name: 'ticket', label: 'Ticket reference (optional)',
          placeholder: 'NET-1042' },
      ],
      confirmLabel: 'Capture and start',
    });
    if (!answer) return;

    const { ok, status, data } = await post(
      `/api/sessions/${encodeURIComponent(tab.sessionId)}/change/start`,
      { note: answer.note || '', ticket: answer.ticket || '' });

    if (!ok) {
      notify({
        severity: status === 409 ? 'info' : 'warning',
        icon: status === 409 ? 'info' : 'error',
        title: status === 409 ? 'A change is already open'
                              : 'The change could not be started',
        body: data.detail || `HTTP ${status}`,
        sticky: status === 409,
      });
      await refresh();
      return;
    }

    await refresh();
    const record = data.change || {};
    notify({
      severity: record.before_id ? 'info' : 'warning',
      // 'history', not 'play_circle': the latter is outside the
      // committed font subset and would render as its own name.
      icon: 'history',
      title: `Change open on ${record.hostname || device}`,
      // The absence of a baseline is said here rather than only in the
      // record at the end, because it changes what the person should do
      // next — they may want to capture by hand before touching anything.
      body: record.before_id
        ? 'The configuration before is captured and pinned.'
        : `No baseline was captured: ${record.capture_error || 'unknown reason'}`,
      sticky: !record.before_id,
    });
  }

  /**
   * Close the window and show the record.
   *
   * Ending is confirmed because it is not reversible from here: the record
   * is produced, the pin is spent, and starting again would measure from a
   * different moment.
   */
  async function end(tab) {
    if (!tab || !tab.sessionId) return;
    const device = deviceOf(tab) || 'this device';

    const yes = await window.shellmateDialog.confirm({
      title: `End the change on ${device}?`,
      body: 'The configuration is captured again and compared with the one '
          + 'from the start. The record opens in the diff window, where you '
          + 'can export it, send it to Jira, or propose the way back.',
      confirmLabel: 'Capture and end',
    });
    if (!yes) return;

    const { ok, status, data } = await post(
      `/api/sessions/${encodeURIComponent(tab.sessionId)}/change/end`);

    if (!ok) {
      notify({
        severity: 'warning', icon: 'error',
        title: 'The change could not be ended',
        body: data.detail || `HTTP ${status}`,
      });
      await refresh();
      return;
    }

    await refresh();
    show(data, tab);
  }

  /** Drop the window without producing a record. */
  async function abandon(tab) {
    if (!tab || !tab.sessionId) return;
    const device = deviceOf(tab) || 'this device';
    const yes = await window.shellmateDialog.confirm({
      title: `Abandon the change on ${device}?`,
      body: 'No record is produced and no comparison is made. Use this when '
          + 'the change was opened on the wrong device, or did not happen.',
      confirmLabel: 'Abandon it',
      danger: true,
    });
    if (!yes) return;

    await post(`/api/sessions/${encodeURIComponent(tab.sessionId)}/change/abandon`);
    await refresh();
    notify({ severity: 'info', icon: 'delete', title: 'Change abandoned',
             body: `No record was kept for ${device}.` });
  }

  /**
   * Put the record on screen.
   *
   * Straight into the diff window, which already understands every field
   * here except `change`, `commands` and `pending` — and drift.js renders
   * those into a block of its own.
   */
  function show(record, tab) {
    if (typeof window.showConfigDiff !== 'function') return;
    window.showConfigDiff(record, {
      session_id: tab && tab.sessionId,
      display_label: (tab && (tab.label || tab.hostname)) || record.hostname,
    });
    // The way back, offered from the baseline this change pinned rather
    // than from whatever the last push happened to leave.
    if (record.old_id && window.shellmateConfigPush
        && window.shellmateConfigPush.offerRestore) {
      window.shellmateConfigPush.offerRestore(tab, record.old_id);
    }
  }

  document.addEventListener('DOMContentLoaded', () => { refresh(); });
  // A change may have been opened or closed in another window against the
  // same data folder, so the cache is refreshed whenever the set of
  // sessions moves rather than only after this window's own actions.
  // On `window`, which is where tabs.js dispatches it.
  window.addEventListener('shellmate:sessions-changed', refresh);

  window.shellmateChange = {
    start, end, abandon, show, refresh, hasOpen, deviceOf,
  };
})();
