/**
 * uptime.js — How long each session has been up.
 *
 * Worth more here than in an ordinary terminal. It is the first thing you want
 * after a device comes back, and the thing you check when a session feels like
 * it may have been idled out by an ACL somewhere in the middle.
 *
 * Three decisions behind the implementation:
 *
 * **Counted in the browser from an absolute timestamp.** The backend stamps
 * `connected_at` when the session is created and sends it once. Nothing ticks
 * over the WebSocket: a per-second message per session would be noise, and a
 * clock driven by message arrival stutters whenever the device goes quiet.
 *
 * **One interval for every tab, not one per tab.** Twenty sessions should cost
 * one timer, and it stops entirely when there is nothing to count.
 *
 * **A disconnected session stops and says what it reached.** "Disconnected
 * after 02:11:04" is the useful form. A tab still counting up on a dead
 * session is telling you something untrue, which is worse than telling you
 * nothing.
 */
(function () {
  'use strict';

  /** sessionId -> { startedMs, endedMs|null } */
  const sessions = new Map();

  let ticker = null;

  document.addEventListener('DOMContentLoaded', () => {
    // Redraw when the active tab changes, so the status bar is right
    // immediately rather than up to a second later.
    window.addEventListener('mate:tab-switched', render);
  });

  /**
   * Start counting for a session.
   *
   * @param {string} sessionId
   * @param {string} connectedAt ISO 8601 from the backend. Falling back to
   *        "now" is deliberate: a missing timestamp should cost a few seconds
   *        of accuracy, not the whole feature.
   */
  function start(sessionId, connectedAt) {
    if (!sessionId) return;
    const parsed = connectedAt ? Date.parse(connectedAt) : NaN;
    sessions.set(sessionId, {
      startedMs: Number.isNaN(parsed) ? Date.now() : parsed,
      endedMs:   null,
    });
    startTicker();
    render();
  }

  /** Freeze a session's timer at the moment it dropped. */
  function stop(sessionId) {
    const entry = sessions.get(sessionId);
    if (!entry || entry.endedMs !== null) return;
    entry.endedMs = Date.now();
    // A frozen clock needs no ticks. The ticker used to stop only when the
    // last session was *forgotten*, so a strip of all-disconnected tabs went
    // on re-rendering the status bar every second (#491).
    if (!anyCounting()) stopTicker();
    render();
  }

  /** Whether any session still has a clock running. */
  function anyCounting() {
    for (const entry of sessions.values()) {
      if (entry.endedMs === null) return true;
    }
    return false;
  }

  /** Resume counting — a reconnect gets a fresh clock, not the old one. */
  function restart(sessionId, connectedAt) {
    sessions.delete(sessionId);
    start(sessionId, connectedAt);
  }

  function forget(sessionId) {
    sessions.delete(sessionId);
    if (!anyCounting()) stopTicker();
    render();
  }

  // One interval, and only while there is something to count and somebody
  // to see it: through visibility.js (#491), it pauses with the window and
  // redraws once on the way back so the clock is right immediately.
  function startTicker() {
    if (ticker) return;
    ticker = window.shellmateVisibility
      ? window.shellmateVisibility.every(render, 1000)
      : { handle: setInterval(render, 1000),
          stop() { clearInterval(this.handle); } };
  }

  function stopTicker() {
    if (ticker) { ticker.stop(); ticker = null; }
  }

  /**
   * Seconds a session has been up, or was up for.
   * @returns {number|null} null if the session is unknown.
   */
  function secondsFor(sessionId) {
    const entry = sessions.get(sessionId);
    if (!entry) return null;
    return Math.max(0, Math.round(((entry.endedMs ?? Date.now()) - entry.startedMs) / 1000));
  }

  /** hh:mm:ss, with days spelled out once a session has run that long. */
  function format(seconds) {
    if (seconds === null || seconds === undefined) return '';
    const days = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const clock = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:` +
                  `${String(s).padStart(2, '0')}`;
    return days ? `${days}d ${clock}` : clock;
  }

  /** The bare duration, "01:05:12", for callers that add their own words (#581). */
  function duration(sessionId) {
    return sessions.has(sessionId) ? format(secondsFor(sessionId)) : '';
  }

  function label(sessionId) {
    const entry = sessions.get(sessionId);
    if (!entry) return '';
    const text = format(secondsFor(sessionId));
    return entry.endedMs === null
      ? `connected ${text}`
      : `disconnected after ${text}`;
  }

  function render() {
    // Beside the connection state, so the line reads "SSH: name | Connected
    // 01:05:12" (#227). Digits only here — the state word belongs to
    // #status-connection-text, and repeating it made the line stutter.
    const el = document.getElementById('status-connection-clock');
    const active = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;

    if (el) {
      const text = active ? format(secondsFor(active.sessionId)) : '';
      el.textContent = text;
      el.classList.toggle('hidden', !text);
    }

    // The tab tooltips are deliberately *not* touched here any more.
    //
    // A native tooltip cannot be updated in place: rewriting `title` while it
    // is showing makes the browser tear it down and rebuild it, so a value
    // that changes once a second flickers under the pointer for as long as
    // you hover. Nothing about that is fixable with styling — anything
    // changing every second simply cannot live in a `title`.
    //
    // The status bar above answers the same question for the session you are
    // looking at, in ordinary text that updates without flicker, which is
    // where a clock belonged in the first place.
  }

  window.shellmateUptime = {
    start, stop, restart, forget, render,
    secondsFor, label, duration,
    _test: { format },
  };
})();
