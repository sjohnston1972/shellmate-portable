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
    // Announced by a screen reader as they arrive (#428), politely — a
    // toast is never worth interrupting what is being read.
    toastHost.setAttribute('role', 'status');
    toastHost.setAttribute('aria-live', 'polite');
    toastHost.setAttribute('aria-atomic', 'false');
    // Inside the terminal pane, so a toast is never over the chat controls.
    (document.getElementById('terminal-pane') || document.body).appendChild(toastHost);

    statusEl = document.getElementById('status-alert');
    // The countdown text is also the dismiss affordance (#265).
    if (statusEl) statusEl.addEventListener('click', _dismissFromStatus);

    // The two moments the dashboard comes and goes.
    window.addEventListener('shellmate:dashboard-changed', syncVisibility);

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
        body:     pending.awaiting_confirmation
          // The typed command was seen, but the device has not yet said the
          // schedule is real — the countdown starts when it does (#248).
          ? `${labelFor(sessionId)} ${KIND_VERB[pending.kind] || 'will act'} if the ` +
            `device confirms it. The countdown starts when it does.`
          : pending.confident
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
      updateBigWarning(sessionId, entry, left);

      if (!worst || rank(severity) > rank(worst.severity) ||
          (left !== null && worst.left !== null && left < worst.left)) {
        worst = { sessionId, pending: entry.pending, left, severity };
      }
    });

    // The warning must not outlive its pending — a cancel or an expiry
    // clears the tracked entry without passing through the branch above.
    if (bigWarning && !tracked.has(bigWarning.sessionId)) removeBigWarning();

    paintStatus(worst);
  }

  // -------------------------------------------------------------------------
  // The last-chance warning (#249)
  //
  // Twenty seconds out, a toast in the corner is no longer proportionate: the
  // device is about to drop, and someone looking at another tab has exactly
  // one chance to notice. This is deliberately the loudest thing in the
  // application — centre-screen, above everything, and it stays until
  // dismissed, jumped to, or the reload happens.
  // -------------------------------------------------------------------------

  const BIG_WARNING_AT = 20;

  let bigWarning = null;   // { sessionId, count } while on screen

  function updateBigWarning(sessionId, entry, left) {
    const due = left !== null && left >= 0 && left <= BIG_WARNING_AT
      && !entry.bigDismissed;

    if (due) {
      if (!bigWarning) showBigWarning(sessionId, entry);
      if (bigWarning && bigWarning.sessionId === sessionId) {
        bigWarning.count.textContent = formatLeft(left);
      }
      return;
    }
    if (bigWarning && bigWarning.sessionId === sessionId) removeBigWarning();
  }

  function showBigWarning(sessionId, entry) {
    const overlay = document.createElement('div');
    overlay.id = 'reload-warning';

    const dialog = document.createElement('div');
    dialog.id = 'reload-warning-dialog';

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined reload-warning-icon';
    icon.textContent = 'warning';

    const title = document.createElement('div');
    title.className = 'reload-warning-title';
    title.textContent =
      `${labelFor(sessionId)} ${KIND_VERB[entry.pending.kind] || 'will act'} in`;

    const count = document.createElement('div');
    count.className = 'reload-warning-count';

    const body = document.createElement('div');
    body.className = 'reload-warning-body';
    body.textContent = entry.pending.source || '';

    const actions = document.createElement('div');
    actions.className = 'reload-warning-actions';

    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'btn-secondary';
    dismiss.textContent = 'Dismiss this warning';
    dismiss.addEventListener('click', () => {
      entry.bigDismissed = true;
      removeBigWarning();
    });

    const jump = document.createElement('button');
    jump.type = 'button';
    jump.className = 'btn-primary';
    jump.textContent = 'Jump to tab';
    jump.addEventListener('click', () => {
      entry.bigDismissed = true;
      removeBigWarning();
      if (typeof window.switchToTabBySessionId === 'function') {
        window.switchToTabBySessionId(sessionId);
      }
    });

    actions.append(dismiss);

    // Calling it off, from the warning itself (#292). Twenty seconds is not
    // long enough to jump to the tab and remember the command, and the
    // command is a property of the platform anyway — the backend sends it
    // with the pending. Absent where the platform has no such command,
    // because a button that types nothing useful into a device that is
    // about to reboot is worse than no button.
    const cancelCommand = entry.pending.cancel_command;
    if (cancelCommand) {
      const stop = document.createElement('button');
      stop.type = 'button';
      stop.className = 'btn-primary reload-warning-stop';
      stop.textContent = `Send "${cancelCommand}"`;
      stop.title = `Types ${cancelCommand} into ${labelFor(sessionId)} and presses return`;
      stop.addEventListener('click', () => {
        const sent = typeof window.sendCommandToSession === 'function'
          && window.sendCommandToSession(sessionId, cancelCommand);
        entry.bigDismissed = true;
        removeBigWarning();
        // The device's own "SHUTDOWN ABORTED" is what actually clears the
        // countdown — this only says the command went, which is the honest
        // claim to make before the device has answered.
        raise({
          severity: sent ? 'info' : 'warning',
          icon:     sent ? 'check_circle' : 'error',
          title:    sent
            ? `Sent "${cancelCommand}" to ${labelFor(sessionId)}`
            : `Could not reach ${labelFor(sessionId)}`,
          body:     sent
            ? 'The countdown clears when the device confirms it is aborted.'
            : 'The session is not connected. Cancel it from the device.',
          sessionId,
          deadline_ms: entry.pending.deadline_ms,
        });
      });
      actions.appendChild(stop);
    }

    actions.appendChild(jump);
    dialog.append(icon, title, count, body, actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    bigWarning = { sessionId, count };
    if (enabled('sound', true)) beep('critical');
  }

  function removeBigWarning() {
    const el = document.getElementById('reload-warning');
    if (el) el.remove();
    bigWarning = null;
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
    statusWorst = worst || null;
    if (!statusEl) return;
    if (!worst) {
      statusEl.textContent = '';
      statusEl.className = '';
      statusEl.title = '';
      statusEl.style.cursor = '';
      return;
    }
    const name = labelFor(worst.sessionId);
    const what = KIND_LABEL[worst.pending.kind] || 'Action';
    statusEl.textContent = worst.left === null
      ? `${what} pending on ${name} (time unknown)`
      : `${what} on ${name} in ${formatLeft(worst.left)}`;
    statusEl.className = `status-alert-${worst.severity}`;
    statusEl.title = 'Click to dismiss this pending action';
    statusEl.style.cursor = 'pointer';
  }

  /** The pending the status bar currently names, for the dismiss click. */
  let statusWorst = null;

  /**
   * Offer to stop tracking what the status bar shows (#265).
   *
   * A pending with no readable time otherwise sits there for the six-hour
   * stale limit with nothing anyone can do about it. Dismissing is local:
   * nothing is sent to the device, and anything genuinely scheduled on it
   * still happens — the dialog says so.
   */
  async function _dismissFromStatus() {
    if (!statusWorst) return;
    const what = (KIND_LABEL[statusWorst.pending.kind] || 'action').toLowerCase();
    const ok = await window.shellmateDialog.confirm({
      title: `Dismiss the pending ${what}?`,
      body:  'ShellMate stops tracking it here. Nothing is sent to the '
             + 'device — anything actually scheduled on it still happens.',
      confirmLabel: 'Dismiss',
    });
    if (ok && typeof window.dismissPendingAction === 'function') {
      window.dismissPendingAction(statusWorst.sessionId);
    }
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
        // A threshold toast is urgent by nature: without this, shouldShow()
        // dropped the 600/300/60s warnings whenever the dashboard was open
        // or another tab had focus — exactly when a pop-up is the only
        // channel left (#250). Only the 10s critical survived.
        deadline_ms: entry.pending.deadline_ms,
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

  /**
   * Whether this alert should be on screen right now (#164).
   *
   * Two rules, and the exception is the point of the feature:
   *
   * - **Not over the dashboard.** Notifications belong to the terminal view.
   *   The stack lives inside #terminal-pane and the dashboard covers that
   *   pane rather than replacing it, which is why they showed through.
   * - **Not from another session** — *unless it carries a deadline*. A
   *   pending reload countdown reaches somebody looking at a different tab
   *   deliberately; that is the whole reason the popup channel exists, and
   *   suppressing it would hide the one alert that most needs to travel.
   */
  function shouldShow(alert) {
    // The exemption comes first, and deliberately outranks the dashboard
    // rule as well as the other-tab one. A device is about to reload; where
    // somebody happens to be looking does not make that less true, and a
    // countdown suppressed because the dashboard was open is the single
    // worst outcome available here.
    if (alert.deadline_ms || alert.severity === 'critical') return true;

    if (typeof window.dashboardVisible === 'function' && window.dashboardVisible()) {
      return false;
    }
    if (!alert.sessionId) return true;

    const active = typeof window.getActiveTab === 'function'
      ? window.getActiveTab() : null;
    return !active || active.sessionId === alert.sessionId;
  }

  /**
   * Hide the stack while the dashboard is in front (#168).
   *
   * shouldShow() only runs when a toast is *created*, so anything raised
   * while connected carried on being painted when the dashboard opened over
   * the pane — the dashboard covers #terminal-pane rather than replacing it,
   * which is what keeps sessions alive, and the stack lives inside that pane
   * above it.
   *
   * The host is hidden rather than the toasts removed: a sticky drift prompt
   * or a reload countdown must still be there when you come back, and
   * dismissing them to satisfy this would trade one fault for a worse one.
   *
   * **Except when something is waiting that must not be missed.** #164 lets a
   * deadline or a critical alert through wherever somebody is looking;
   * hiding the whole host would suppress exactly those, so the host stays
   * visible whenever it holds one.
   */
  function syncVisibility() {
    if (!toastHost) return;
    const onDashboard = typeof window.dashboardVisible === 'function'
      && window.dashboardVisible();
    const holdsUrgent = toastHost.querySelector('.alert-critical, [data-urgent="1"]');
    toastHost.classList.toggle('alerts-hidden', Boolean(onDashboard) && !holdsUrgent);
  }

  function toast(alert) {
    if (!toastHost) return;
    if (!shouldShow(alert)) return;

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
    close.addEventListener('click', () => { el.remove(); syncVisibility(); });

    el.append(icon, text);

    // An offer needs the thing being offered attached to it. Without this,
    // anything with an action had to build its own floating element — which
    // is how ShellMate ended up with three of them in the same corner,
    // different sizes, overlapping, one hiding another's button.
    if (alert.action && alert.action.label) {
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'alert-toast-action';
      action.textContent = alert.action.label;
      action.addEventListener('click', (e) => {
        e.stopPropagation();
        el.remove();
        try { alert.action.onClick(); } catch (_) { /* the toast still goes */ }
      });
      el.appendChild(action);
    }

    el.appendChild(close);

    // Clicking the toast takes you to the device it is about, which is what
    // anyone reading it is about to want.
    if (alert.sessionId && typeof window.switchToTabBySessionId === 'function') {
      text.style.cursor = 'pointer';
      text.addEventListener('click', () => {
        window.switchToTabBySessionId(alert.sessionId);
        el.remove();
      });
    }

    // Marked so syncVisibility() can keep the stack up for it even on the
    // dashboard — the same exemption shouldShow() applies on the way in.
    if (alert.deadline_ms || alert.severity === 'critical') {
      el.dataset.urgent = '1';
    }

    toastHost.appendChild(el);
    syncVisibility();

    // A stack that grows without limit stops being readable and starts being
    // a wall. Oldest goes first — the newest alert is the current one.
    const stack = [...toastHost.children];
    if (stack.length > A('alerts.max_toasts', MAX_TOASTS_DEFAULT)) {
      stack.slice(0, stack.length - A('alerts.max_toasts', MAX_TOASTS_DEFAULT)).forEach(old => old.remove());
    }

    // A question waits to be answered. "This device has changed — would you
    // like to see the difference?" timing out after twelve seconds asks
    // something and then withdraws it, which is worse than not asking.
    if (alert.sticky) return el;

    // Critical alerts linger, because the point of the last warning is that it
    // is still there when someone looks back at the machine. Not forever
    // though: one pinned across the screen an hour later is only in the way.
    // `seconds` lets one notice outlive the rest without being permanent —
    // an offer worth noticing but not worth living in the corner (#148).
    const life = alert.severity === 'critical'
      ? 120
      : (Number(alert.seconds) || A('alerts.toast_seconds', 12));
    setTimeout(() => el.remove(), life * 1000);
    return el;
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
