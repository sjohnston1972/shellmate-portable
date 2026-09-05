/**
 * feedback.js — The bug / feature-request reporter (#370).
 *
 * A sidebar entry that opens a report form. Not an AI feature — it
 * posts to /api/feedback, which relays the report to a GitHub issue the
 * maintainer reviews. The form is deliberately tiny: type, title,
 * description. Everything attached beyond that (platform, build) is stated
 * on the form itself, because a feedback box that quietly gathers things is
 * how trust in the rest of the application dies.
 *
 * The launcher is the Feedback entry in the sidebar (index.html); the panel
 * itself is built here, so everything about what a report contains stays in
 * one file.
 */
(function () {
  'use strict';

  let panel = null;
  let kind = 'bug';

  document.addEventListener('DOMContentLoaded', () => {
    // A sidebar entry beside Support, not a floating chat head — one rail
    // holds everything that is not a session, and a second floating control
    // was the inconsistency.
    const link = document.getElementById('sidebar-link-feedback');
    if (link) link.addEventListener('click', (e) => { e.preventDefault(); toggle(); });

    // A fault from the last run, offered once (#568). After the rest of
    // the interface has settled: somebody who has just reopened ShellMate
    // after it fell over is trying to get back to a device.
    setTimeout(checkForCrashes, 2500);
  });

  function toggle() {
    if (panel) { closePanel(); return; }
    // The sidebar entry always opens a fresh report. A crash left selected
    // from a previous open would file the next typed bug as a crash.
    if (kind === 'crash') kind = 'bug';
    pendingCrash = null;
    panel = buildPanel();
    document.body.appendChild(panel);
    panel.querySelector('#feedback-title').focus();
  }

  function closePanel() {
    if (panel) panel.remove();
    panel = null;
  }

  function buildPanel() {
    const box = document.createElement('div');
    box.id = 'feedback-panel';

    const heading = document.createElement('div');
    heading.className = 'feedback-heading';
    heading.textContent = 'Help make ShellMate better';

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'feedback-close';
    closeBtn.title = 'Close';
    closeBtn.innerHTML = '<span class="material-symbols-outlined">close</span>';
    closeBtn.addEventListener('click', closePanel);
    heading.appendChild(closeBtn);

    const welcome = document.createElement('p');
    welcome.className = 'feedback-welcome';
    welcome.textContent =
      'ShellMate is still in active development — please help make it '
      + 'better by reporting any bugs or feature requests.';

    // Two buttons, not a dropdown: the choice is binary and should cost one
    // click, and which is selected should be visible without opening anything.
    const kinds = document.createElement('div');
    kinds.className = 'feedback-kinds';
    const kindButtons = {};
    // A crash is not one of the two things a person chooses between. It
    // says what it is and stays what it is: a picker here would let
    // somebody file a recorded fault as a feature request by mis-clicking,
    // and the body would still be a traceback.
    const KINDS = kind === 'crash'
      ? [['crash', 'bug_report', 'Crash report']]
      : [['bug', 'bug_report', 'Bug'], ['feature', 'lightbulb', 'Feature idea']];
    KINDS
      .forEach(([value, icon, label]) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'feedback-kind' + (kind === value ? ' on' : '');
        b.innerHTML = `<span class="material-symbols-outlined">${icon}</span>`;
        b.appendChild(document.createTextNode(label));
        b.addEventListener('click', () => {
          kind = value;
          Object.entries(kindButtons).forEach(([v, btn]) =>
            btn.classList.toggle('on', v === value));
        });
        kindButtons[value] = b;
        kinds.appendChild(b);
      });

    const title = document.createElement('input');
    title.id = 'feedback-title';
    title.type = 'text';
    title.maxLength = 200;
    title.placeholder = 'One line saying what it is';
    title.autocomplete = 'off';
    title.spellcheck = false;

    const detail = document.createElement('textarea');
    detail.id = 'feedback-detail';
    detail.rows = 5;
    detail.maxLength = 5000;
    detail.placeholder = 'What happened, or what you would like to see. '
      + 'For a bug: what you did, and what you expected instead.';

    // Said plainly, because it is the whole privacy story: what you type,
    // the platform line, whether this is the portable build — and nothing
    // from any terminal session, ever.
    const note = document.createElement('p');
    note.className = 'feedback-note';
    note.textContent = kind === 'crash'
      ? crashNote()
      : 'Sends only what you type here, plus your Windows '
        + 'version and the build type. Nothing from your terminal sessions '
        + 'is ever attached.';

    const status = document.createElement('div');
    status.className = 'feedback-status';

    const actions = document.createElement('div');
    actions.className = 'feedback-actions';

    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'btn-primary';
    send.textContent = 'Send';
    send.addEventListener('click', () => submit(send, title, detail, status));
    actions.appendChild(send);

    // Offered beside Send rather than only on the toast: somebody who
    // opened this to read what was recorded has to be able to decide
    // *after* reading it, which is the entire point of showing it.
    if (kind === 'crash' && pendingCrash) {
      const drop = document.createElement('button');
      drop.type = 'button';
      drop.className = 'btn-tertiary';
      drop.textContent = 'Discard this report';
      drop.addEventListener('click', async () => {
        await discardCrash(pendingCrash.file);
        pendingCrash = null;
        closePanel();
      });
      actions.appendChild(drop);
    }

    box.append(heading, welcome, kinds, title, detail, note, actions, status);
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); closePanel(); }
    });
    return box;
  }

  async function submit(send, title, detail, status) {
    if (!title.value.trim()) {
      status.textContent = 'Give it a one-line title first.';
      status.className = 'feedback-status feedback-error';
      title.focus();
      return;
    }

    send.disabled = true;
    status.className = 'feedback-status';
    status.textContent = 'Sending…';

    let data = null;
    try {
      const res = await fetch('/api/feedback', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          kind, title: title.value, description: detail.value,
        }),
      });
      data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not send it.');
    } catch (e) {
      status.className = 'feedback-status feedback-error';
      status.textContent = e.message;
      send.disabled = false;
      return;
    }

    send.disabled = false;
    title.value = '';
    detail.value = '';

    // Queued counts as dealt with too. The outbox will send it, and
    // offering the same fault again on the next launch would have somebody
    // send it twice to be sure.
    if (pendingCrash) {
      discardCrash(pendingCrash.file);
      pendingCrash = null;
    }

    if (data.status === 'sent') {
      status.className = 'feedback-status feedback-ok';
      status.textContent = 'Sent — thank you!';
      setTimeout(closePanel, 1800);
      return;
    }

    // Queued, not sent — say so, and offer the clipboard so someone on an
    // air-gapped network can still get the report out by mail.
    status.className = 'feedback-status';
    status.textContent = 'Saved locally — it will be sent when ShellMate '
      + 'can next reach the reporting service. Or copy it to paste into '
      + 'an email: ';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'feedback-copy';
    copy.textContent = 'Copy report';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(data.text || '');
        copy.textContent = 'Copied';
      } catch (_) {
        copy.textContent = 'Could not copy';
      }
    });
    status.appendChild(copy);
  }

  // -------------------------------------------------------------------------
  // Crashes (#568)
  //
  // A fault ShellMate recorded itself, offered on the next launch. It is a
  // report like any other from `submit` down — same endpoint, same outbox,
  // same relay — and everything specific to it is here, above that line.
  //
  // **The whole text is shown, and it is the text that is sent.** Not a
  // summary of it, not a redacted-for-display version of a fuller one. This
  // is the only kind of report whose body ShellMate wrote rather than the
  // user, which is exactly why nothing may leave that they have not read.
  //
  // **Nothing is ever sent automatically**, and there is no setting that
  // makes it. `feedback.report_crashes` governs whether the offer appears.
  // The file is written either way, so turning it off loses the prompt, not
  // the evidence.
  // -------------------------------------------------------------------------

  /** The crash being offered, once it has been fetched in full. */
  let pendingCrash = null;

  /**
   * Ask, once, on the launch after a fault.
   *
   * Deliberately not a modal. Somebody who has just reopened ShellMate
   * after it fell over is trying to get back to a device, and a dialog
   * across the middle of the screen is in the way of the thing they came
   * back to do. A toast waits.
   */
  async function checkForCrashes() {
    let data;
    try {
      const res = await fetch('/api/crashes');
      data = await res.json();
    } catch (_) {
      return;
    }
    if (!data.ask || !(data.reports || []).length) return;

    const newest = data.reports[0];
    const others = data.reports.length - 1;

    if (!window.shellmateAlerts) return;
    window.shellmateAlerts.notify({
      severity: 'warning',
      icon: 'bug_report',
      title: 'ShellMate hit a fault last time',
      body: `${newest.exception || 'A fault was recorded'}`
          + `${others ? ` (and ${others} other${others === 1 ? '' : 's'})` : ''}`
          + '. Read what was recorded and decide whether to send it.',
      // A question waits to be answered. Twelve seconds asks this and then
      // withdraws it, which is worse than not asking — and the fault has
      // already happened, so there is no hurry to reply.
      sticky: true,
      action: { label: 'Review', onClick: () => openForCrash(newest.file) },
    });
  }

  /** Open the report form on a recorded fault. */
  async function openForCrash(name) {
    let data;
    try {
      const res = await fetch(`/api/crashes/${encodeURIComponent(name)}`);
      data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'It could not be read.');
    } catch (e) {
      if (window.shellmateAlerts) window.shellmateAlerts.notify({
        global: true, icon: 'close', title: 'That report could not be read',
        body: String(e.message || e),
      });
      return;
    }

    pendingCrash = data;
    if (panel) closePanel();
    kind = 'crash';
    panel = buildPanel();
    document.body.appendChild(panel);

    panel.querySelector('#feedback-title').value = data.title || 'Crash';
    const detail = panel.querySelector('#feedback-detail');
    // The cap is the server's, and the description arrives already at it.
    // Raising maxLength here rather than truncating in the browser: a
    // preview that shows less than what will be sent is the one thing this
    // must not do.
    detail.maxLength = 100000;
    detail.value = data.description || '';
    detail.rows = 14;
  }

  async function discardCrash(name) {
    try {
      await fetch(`/api/crashes/${encodeURIComponent(name)}`,
                  { method: 'DELETE' });
    } catch (_) { /* it will be offered again, which is the safe direction */ }
  }

  /**
   * What the panel says when it is showing a fault rather than a form.
   *
   * The ordinary note promises that nothing from a terminal session is ever
   * attached. That promise still holds here and is worth restating, because
   * this is the report where somebody would most reasonably wonder.
   */
  function crashNote() {
    return 'This is exactly what will be sent: the fault, where ShellMate '
         + 'was when it happened, and the last 50 lines of its own log — '
         + 'all with passwords and community strings masked. Nothing from '
         + 'your terminal sessions is included. Edit or delete anything you '
         + 'would rather not send.';
  }

})();
