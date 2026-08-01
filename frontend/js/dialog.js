/**
 * dialog.js — ShellMate's own confirm, prompt and alert.
 *
 * The browser's `confirm()` was doing the most consequential work in the
 * application: the broadcast preview that lists every command against every
 * device, the master-password warning that says there is no recovery, the
 * file delete that cannot be undone. All of them rendered as an unstyled grey
 * box with the page's URL above it, which is the wrong shape here for four
 * separate reasons:
 *
 *   - **It cannot be styled**, so the moments that most need to look
 *     deliberate were the only ones that did not look like ShellMate at all.
 *   - **Whitespace was the only formatting available.** The broadcast preview
 *     built a numbered command list and a device list out of `\n` and two
 *     spaces, because that is all a native dialog accepts. It wanted a list.
 *   - **It blocks the event loop.** Nothing renders and no WebSocket message
 *     is processed while one is open — so a device announcing `SHUTDOWN in
 *     0:00:30` would not reach the alert system until somebody answered a
 *     question about something else entirely.
 *   - **It is not even consistent.** In the native window it is WebView2's
 *     dialog; in a browser it is that browser's. What a user saw depended on
 *     how they had launched the application.
 *
 * Promise-based so a call site stays one line, because a helper that makes
 * the call site worse is a helper people quietly stop using:
 *
 *     if (!await shellmateDialog.confirm({ title: 'Close core-sw-01?' })) return;
 *
 * Scope, deliberately: this replaces the *native* calls. The three hand-built
 * modals — connection, paste, vault — keep their own markup. Half-migrating
 * them would leave three patterns where there were two.
 */
(function () {
  'use strict';

  let overlay = null;
  let settle = null;
  let previouslyFocused = null;

  /**
   * Ask a yes/no question.
   *
   * @param {object} opts
   * @param {string} opts.title         The question. Required.
   * @param {string} [opts.body]        A paragraph beneath it.
   * @param {Array}  [opts.list]        Lines to show as a list — what the
   *                                    native version had to fake with "\n".
   * @param {string} [opts.confirmLabel] Defaults to "Confirm".
   * @param {string} [opts.cancelLabel]  Defaults to "Cancel".
   * @param {boolean} [opts.danger]     Style the confirm button as
   *                                    destructive. Use it when the answer
   *                                    cannot be taken back.
   * @returns {Promise<boolean>}
   */
  function confirm(opts) {
    return open({ ...opts, kind: 'confirm' });
  }

  /**
   * Ask for a line of text.
   *
   * @param {object} opts
   * @param {boolean} [opts.password] Mask what is typed.
   * @returns {Promise<string|null>} null if cancelled, so "" stays a
   *          distinguishable answer from "no answer".
   */
  function prompt(opts) {
    return open({ ...opts, kind: 'prompt' });
  }

  /** Say something that only needs acknowledging. */
  function alert(opts) {
    return open({ ...opts, kind: 'alert' });
  }

  // -------------------------------------------------------------------------

  function open(opts) {
    // One at a time. A second dialog over the first would leave the first
    // unresolved forever, and its caller waiting on a promise that never
    // settles.
    if (overlay) close(cancelValue(opts.kind));

    return new Promise((resolve) => {
      settle = resolve;
      previouslyFocused = document.activeElement;
      build(opts);
    });
  }

  function cancelValue(kind) {
    return kind === 'prompt' ? null : false;
  }

  function build(opts) {
    overlay = document.createElement('div');
    overlay.className = 'sm-dialog-overlay';
    overlay.dataset.kind = opts.kind;

    const box = document.createElement('div');
    box.className = 'sm-dialog';
    box.setAttribute('role', opts.kind === 'alert' ? 'alertdialog' : 'dialog');
    box.setAttribute('aria-modal', 'true');

    const heading = document.createElement('h2');
    heading.className = 'sm-dialog-title';
    heading.id = 'sm-dialog-title';
    // textContent throughout — a device name, a file path or a snippet name
    // reaches this and none of them are ours to trust as markup.
    heading.textContent = opts.title || '';
    box.setAttribute('aria-labelledby', heading.id);
    box.appendChild(heading);

    if (opts.body) {
      const body = document.createElement('p');
      body.className = 'sm-dialog-body';
      body.textContent = opts.body;
      box.appendChild(body);
    }

    if (opts.list && opts.list.length) {
      box.appendChild(renderList(opts.list));
    }

    let input = null;
    if (opts.kind === 'prompt') {
      const label = document.createElement('label');
      label.className = 'sm-dialog-label';
      label.textContent = opts.label || '';
      label.htmlFor = 'sm-dialog-input';

      input = document.createElement('input');
      // A password is masked here for the same reason it is masked in a form:
      // the person typing it is not always the only person in the room.
      input.type = opts.password ? 'password' : 'text';
      input.id = 'sm-dialog-input';
      input.className = 'sm-dialog-input';
      input.value = opts.value || '';
      input.autocomplete = 'off';
      input.spellcheck = false;

      if (opts.label) box.appendChild(label);
      box.appendChild(input);
    }

    if (opts.note) {
      const note = document.createElement('p');
      note.className = 'sm-dialog-note';
      note.textContent = opts.note;
      box.appendChild(note);
    }

    const actions = document.createElement('div');
    actions.className = 'sm-dialog-actions';

    let cancel = null;
    if (opts.kind !== 'alert') {
      cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'btn-secondary';
      cancel.textContent = opts.cancelLabel || 'Cancel';
      cancel.addEventListener('click', () => close(cancelValue(opts.kind)));
      actions.appendChild(cancel);
    }

    const accept = document.createElement('button');
    accept.type = 'button';
    // A destructive answer should not wear the same button as an ordinary
    // one. Reading the label is what people skip when they are in a hurry,
    // and being in a hurry is when this matters.
    accept.className = opts.danger ? 'btn-danger' : 'btn-primary';
    accept.textContent = opts.confirmLabel
      || (opts.kind === 'alert' ? 'OK' : opts.kind === 'prompt' ? 'Save' : 'Confirm');
    accept.addEventListener('click', () => {
      close(opts.kind === 'prompt' ? (input ? input.value : '') : true);
    });
    actions.appendChild(accept);

    box.appendChild(actions);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // Clicking the backdrop is a cancel, like every other overlay here.
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) close(cancelValue(opts.kind));
    });

    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        close(cancelValue(opts.kind));
        return;
      }
      if (e.key === 'Enter' && (opts.kind !== 'confirm' || e.target !== cancel)) {
        e.preventDefault();
        accept.click();
        return;
      }
      if (e.key === 'Tab') trapFocus(e, box);
    });

    // The field first where there is one, so a prompt can be answered
    // without reaching for the mouse. Otherwise the confirm button — except
    // when it is destructive, where landing on Cancel is the safer default
    // for anyone who hits Enter on reflex.
    const initial = input || (opts.danger && cancel ? cancel : accept);
    setTimeout(() => { initial.focus(); if (input) input.select(); }, 0);
  }

  function renderList(items) {
    const list = document.createElement('ul');
    list.className = 'sm-dialog-list';
    items.forEach(entry => {
      const li = document.createElement('li');
      if (entry && typeof entry === 'object') {
        // { text, detail } — the command, and which device it goes to.
        const text = document.createElement('span');
        text.className = 'sm-dialog-list-text';
        text.textContent = entry.text || '';
        li.appendChild(text);
        if (entry.detail) {
          const detail = document.createElement('span');
          detail.className = 'sm-dialog-list-detail';
          detail.textContent = entry.detail;
          li.appendChild(detail);
        }
        if (entry.mono) li.classList.add('sm-dialog-list-mono');
      } else {
        li.textContent = String(entry);
      }
      list.appendChild(li);
    });
    return list;
  }

  /**
   * Keep Tab inside the dialog.
   *
   * Without this, tabbing walks out into the terminal behind — which is still
   * a live session, and typing into it because a dialog did not hold focus is
   * not a mistake worth making possible.
   */
  function trapFocus(e, box) {
    const focusable = [...box.querySelectorAll('button, input, [tabindex]:not([tabindex="-1"])')]
      .filter(el => !el.disabled && el.offsetParent !== null);
    if (!focusable.length) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function close(value) {
    if (overlay) overlay.remove();
    overlay = null;

    // Focus back where it came from: a Settings button, a tab, a file row.
    // Losing it to <body> means the next keystroke goes nowhere.
    if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
      try { previouslyFocused.focus(); } catch (_) { /* it may have gone */ }
    }
    previouslyFocused = null;

    const resolve = settle;
    settle = null;
    if (resolve) resolve(value);
  }

  window.shellmateDialog = { confirm, prompt, alert };
})();
