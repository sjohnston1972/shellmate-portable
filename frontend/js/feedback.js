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
  });

  function toggle() {
    if (panel) { closePanel(); return; }
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
    [['bug', 'bug_report', 'Bug'], ['feature', 'lightbulb', 'Feature idea']]
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
    note.textContent = 'Sends only what you type here, plus your Windows '
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
})();
