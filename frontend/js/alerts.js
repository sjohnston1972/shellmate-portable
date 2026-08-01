/**
 * alerts.js — Telling someone that something is about to happen to a device.
 *
 * The backend decides *what* is pending (see backend/alerts.py); this decides
 * what a pending reboot looks like and sounds like.
 *
 * A general alert has a severity, an optional session and an optional
 * deadline. A pending reload is the first thing to raise one, and is the
 * reason the deadline exists: `reload in 10` deserves a clock, not a line of
 * output that scrolled past four minutes ago.
 *
 * Four channels, each independently switchable, because how much interruption
 * is appropriate depends entirely on where you are working:
 *
 *   Countdown  A clock on the tab and in the status bar. Always on — it is
 *              the information itself rather than an interruption.
 *   Flash      The tab pulses as the deadline nears.
 *   Sound      A short tone, synthesised here. No audio file to fetch, since
 *              ShellMate has to work with no network at all.
 *   Popup      A toast at thresholds, which is the one that reaches someone
 *              looking at a different tab.
 *
 * The countdown is computed in the browser from an absolute deadline the
 * backend sent once. Nothing ticks over the socket: a per-second message per
 * session would be noise, and a countdown driven by message arrival stutters
 * whenever the device goes quiet.
 */
(function () {
  'use strict';

  /** Shorthand for a Stockton value. */
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

  /** Seconds remaining at which each escalation fires, loudest last. */
  const DEFAULT_THRESHOLDS = '600,300,60,10';

  /**
   * Seconds remaining at which each escalation fires, loudest last.
   *
   * Read from Stockton so somebody who wants a nudge half an hour out can have
   * one. A malformed list falls back to the shipped one rather than leaving a
   * pending reload unannounced — which is the failure that matters here.
   */
  function thresholds() {
    const raw = String(A('alerts.thresholds', DEFAULT_THRESHOLDS));
    const seconds = raw.split(',')
      .map(s => parseInt(s.trim(), 10))
      .filter(n => Number.isFinite(n) && n > 0)
      .sort((a, b) => b - a);

    if (!seconds.length) {
      return thresholdsFrom(DEFAULT_THRESHOLDS);
    }
    return seconds.map((at, i) => ({
      at,
      // The last one is the one you must not miss.
      severity: i === seconds.length - 1 ? 'critical'
              : i === 0 ? 'info' : 'warning',
    }));
  }

  function thresholdsFrom(text) {
    return text.split(',').map((s, i, all) => ({
      at: parseInt(s, 10),
      severity: i === all.length - 1 ? 'critical' : i === 0 ? 'info' : 'warning',
    }));
  }

  /** Most toasts on screen at once. */
  const MAX_TOASTS_DEFAULT = 3;

  const KIND_LABEL = {
    'reload':         'Reload',
    'commit-confirm': 'Rollback',
  };

  const KIND_VERB = {
    'reload':         'will reload',
    'commit-confirm': 'will roll back this commit',
  };

  /** sessionId → { pending, label, fired: Set<number> } */
  const tracked = new Map();

  let toastHost, statusEl, ticker = null, audio = null;

  document.addEventListener('DOMContentLoaded', () => {
    toastHost = document.createElement('div');
    toastHost.id = 'alert-toasts';
    // Inside the terminal pane, so a toast is never over the chat controls.
    (document.getElementById('terminal-pane') || document.body).appendChild(toastHost);

    statusEl = document.getElementById('status-alert');

    window.addEventListener('shellmate:pending-action', (e) => {
      const { sessionId, pending } = e.detail || {};
      if (!sessionId) return;
      set(sessionId, pending);
    });

    // A closed tab cannot have anything pending on it any more.
    window.addEventListener('mate:tab-switched', prune);
    setInterval(prune, 5000);
  });

  function settings() {
    return (window.shellmateSettings || {}).alerts || {};
  }

  function enabled(channel, fallback) {
    const value = settings()[channel];
    return value === undefined ? fallback : Boolean(value);
  }

  function labelFor(sessionId) {
    const tab = (window.getOpenTabs ? window.getOpenTabs() : [])
      .find(t => t.sessionId === sessionId);
    return tab ? (tab.label || tab.hostname || sessionId.slice(0, 8))
               : sessionId.slice(0, 8);
  }

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  function set(sessionId, pending) {
    const existing = tracked.get(sessionId);

    if (!pending) {
      if (existing) {
        tracked.delete(sessionId);
        clearTabBadge(sessionId);
        // Worth saying out loud: a countdown that simply disappears leaves
        // someone wondering whether it fired or was cancelled.
        raise({
          severity: 'info',
          title:    `${labelFor(sessionId)}: nothing pending`,
          body:     'The scheduled action was cancelled or has passed.',
          sessionId,
        });
      }
      render();
      return;
    }

    // Keep the fired thresholds when the same action is merely re-timed by the
    // device, or every banner IOS prints would sound the alarm again.
    const fired = (existing && existing.pending.kind === pending.kind)
      ? existing.fired : new Set();

    tracked.set(sessionId, { pending, fired });

    if (!existing || existing.pending.kind !== pending.kind) {
      raise({
        severity: 'warning',
        title:    `${labelFor(sessionId)}: ${KIND_LABEL[pending.kind] || 'action'} pending`,
        body:     pending.confident
          ? `${labelFor(sessionId)} ${KIND_VERB[pending.kind] || 'will act'} — see the countdown.`
          : `${labelFor(sessionId)} ${KIND_VERB[pending.kind] || 'will act'}, but the ` +
            `time was not readable. No countdown is shown rather than a wrong one.`,
        sessionId,
      });
    }

    render();
    startTicking();
  }

  function prune() {
    const live = new Set((window.getOpenTabs ? window.getOpenTabs() : [])
      .map(t => t.sessionId));
    let changed = false;
    [...tracked.keys()].forEach(id => {
      if (!live.has(id)) { tracked.delete(id); changed = true; }
    });
    if (changed) render();
  }

  function startTicking() {
    if (ticker) return;
    ticker = setInterval(() => {
      if (!tracked.size) { clearInterval(ticker); ticker = null; return; }
      render();
    }, 1000);
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  function secondsLeft(pending) {
    if (!pending.deadline_ms) return null;
    return Math.round((pending.deadline_ms - Date.now()) / 1000);
  }

  function formatLeft(seconds) {
    if (seconds < 0) return 'now';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return `${h}h${String(m).padStart(2, '0')}m`;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function render() {
    let worst = null;

    tracked.forEach((entry, sessionId) => {
      const left = secondsLeft(entry.pending);
      const severity = severityFor(left);

      paintTab(sessionId, entry.pending, left, severity);
      escalate(sessionId, entry, left, severity);

      if (!worst || rank(severity) > rank(worst.severity) ||
          (left !== null && worst.left !== null && left < worst.left)) {
        worst = { sessionId, pending: entry.pending, left, severity };
      }
    });

    paintStatus(worst);
  }

  function rank(severity) {
    return { info: 0, warning: 1, critical: 2 }[severity] || 0;
  }

  function severityFor(left) {
    if (left === null) return 'warning';
    if (left <= 60) return 'critical';
    if (left <= A('alerts.flash_within', 300)) return 'warning';
    return 'info';
  }

  function paintTab(sessionId, pending, left, severity) {
    const tabEl = document.querySelector(`.tab[data-session-id="${CSS.escape(sessionId)}"]`);
    if (!tabEl) return;

    let badge = tabEl.querySelector('.tab-countdown');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'tab-countdown';
      // Before the close button, so the × stays where the muscle memory is.
      tabEl.insertBefore(badge, tabEl.querySelector('.tab-close'));
    }

    badge.textContent = left === null ? '!' : formatLeft(left);
    badge.title = `${KIND_LABEL[pending.kind] || 'Action'} pending: ${pending.source}`;
    badge.className = `tab-countdown tab-countdown-${severity}`;

    // Flashing is for the last stretch. Earlier than that it is decoration,
    // and a tab that pulses for ten minutes is a tab people stop seeing.
    const shouldFlash = enabled('flash_tab', true) &&
                        left !== null && left <= A('alerts.flash_within', 300) && !reduceMotion();
    tabEl.classList.toggle('tab-alerting', shouldFlash);
    tabEl.classList.toggle('tab-alerting-critical', shouldFlash && severity === 'critical');
  }

  function clearTabBadge(sessionId) {
    const tabEl = document.querySelector(`.tab[data-session-id="${CSS.escape(sessionId)}"]`);
    if (!tabEl) return;
    const badge = tabEl.querySelector('.tab-countdown');
    if (badge) badge.remove();
    tabEl.classList.remove('tab-alerting', 'tab-alerting-critical');
  }

  function paintStatus(worst) {
    if (!statusEl) return;
    if (!worst) {
      statusEl.textContent = '';
      statusEl.className = '';
      return;
    }
    const name = labelFor(worst.sessionId);
    const what = KIND_LABEL[worst.pending.kind] || 'Action';
    statusEl.textContent = worst.left === null
      ? `${what} pending on ${name} (time unknown)`
      : `${what} on ${name} in ${formatLeft(worst.left)}`;
    statusEl.className = `status-alert-${worst.severity}`;
  }

  function reduceMotion() {
    return Boolean(settings().reduce_motion) ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // -------------------------------------------------------------------------
  // Escalation
  // -------------------------------------------------------------------------

  function escalate(sessionId, entry, left, severity) {
    if (left === null) return;

    thresholds().forEach(threshold => {
      if (left > threshold.at || entry.fired.has(threshold.at)) return;
      // Do not fire every threshold at once for something already overdue.
      if (left < 0) { entry.fired.add(threshold.at); return; }

      entry.fired.add(threshold.at);
      raise({
        severity: threshold.severity,
        title:    `${labelFor(sessionId)} in ${formatLeft(left)}`,
        body:     `${KIND_LABEL[entry.pending.kind] || 'Action'}: ${entry.pending.source}`,
        sessionId,
        sound:    true,
      });
    });
  }

  // -------------------------------------------------------------------------
  // Channels
  // -------------------------------------------------------------------------

  /**
   * Raise an alert. The single entry point, so anything else that wants to
   * interrupt someone — a dropped session, a blocked command — arrives the
   * same way and obeys the same switches.
   */
  function raise(alert) {
    if (enabled('popup', true)) toast(alert);
    if (alert.sound && enabled('sound', true)) beep(alert.severity);
  }

  /**
   * A quiet confirmation that something finished — a configuration captured,
   * a file written.
   *
   * Deliberately not routed through raise(). The switches in Settings → Alerts
   * govern how loudly ShellMate interrupts you about *a device that is about
   * to do something*, and borrowing them to also suppress "your capture was
   * saved" would make one of them mean two things. This never beeps and never
   * lingers.
   */
  function notify(what) {
    toast({ severity: 'info', icon: 'check_circle', ...what });
  }

  function toast(alert) {
    if (!toastHost) return;

    const el = document.createElement('div');
    el.className = `alert-toast alert-${alert.severity}`;

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = alert.icon
                     || (alert.severity === 'critical' ? 'error'
                       : alert.severity === 'warning' ? 'warning' : 'help');

    const text = document.createElement('div');
    text.className = 'alert-toast-text';

    const title = document.createElement('div');
    title.className = 'alert-toast-title';
    // textContent — a device name is not ours to trust as markup.
    title.textContent = alert.title;

    const body = document.createElement('div');
    body.className = 'alert-toast-body';
    body.textContent = alert.body || '';

    text.append(title, body);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'alert-toast-close';
    close.innerHTML = '<span class="material-symbols-outlined">close</span>';
    close.addEventListener('click', () => el.remove());

    el.append(icon, text, close);

    // Clicking the toast takes you to the device it is about, which is what
    // anyone reading it is about to want.
    if (alert.sessionId && typeof window.switchToTabBySessionId === 'function') {
      text.style.cursor = 'pointer';
      text.addEventListener('click', () => {
        window.switchToTabBySessionId(alert.sessionId);
        el.remove();
      });
    }

    toastHost.appendChild(el);

    // A stack that grows without limit stops being readable and starts being
    // a wall. Oldest goes first — the newest alert is the current one.
    const stack = [...toastHost.children];
    if (stack.length > A('alerts.max_toasts', MAX_TOASTS_DEFAULT)) {
      stack.slice(0, stack.length - A('alerts.max_toasts', MAX_TOASTS_DEFAULT)).forEach(old => old.remove());
    }

    // Critical alerts linger, because the point of the last warning is that it
    // is still there when someone looks back at the machine. Not forever
    // though: one pinned across the screen an hour later is only in the way.
    setTimeout(() => el.remove(), alert.severity === 'critical' ? 120000 : A('alerts.toast_seconds', 12) * 1000);
  }

  /**
   * A short tone, synthesised rather than fetched.
   *
   * ShellMate has to work on an air-gapped network, so there is no audio file
   * to load — and bundling one would cost more than the twenty lines this
   * takes. Two rising notes for critical, one for anything else.
   */
  function beep(severity) {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audio = audio || new Ctx();
      // Browsers suspend audio until the page has been interacted with.
      if (audio.state === 'suspended') audio.resume();

      const notes = severity === 'critical' ? [880, 1174] : [660];
      notes.forEach((frequency, index) => {
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.type = 'sine';
        osc.frequency.value = frequency;

        const start = audio.currentTime + index * 0.18;
        // Ramped rather than switched, because a square-edged gain change
        // clicks audibly.
        gain.gain.setValueAtTime(0, start);
        gain.gain.linearRampToValueAtTime(
          A('alerts.tone_volume', 0.2) * 0.7, start + 0.02);
        gain.gain.linearRampToValueAtTime(0, start + 0.16);

        osc.connect(gain).connect(audio.destination);
        osc.start(start);
        osc.stop(start + 0.18);
      });
    } catch (_) { /* no audio device, or blocked; the toast still shows */ }
  }

  // Exposed so other producers can raise alerts, and for testing.
  window.shellmateAlerts = {
    raise,
    notify,
    pending: () => [...tracked.entries()].map(([sessionId, e]) => ({
      sessionId, ...e.pending, seconds_left: secondsLeft(e.pending),
    })),
    _test: { formatLeft, severityFor, beep },
  };
})();
