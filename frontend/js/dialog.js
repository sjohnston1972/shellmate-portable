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
   * @param {Node}   [opts.content]     An element beneath that, for a choice
   *                                    a field cannot express.
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

  /**
   * Ask for several things at once.
   *
   * `prompt` takes one input, so anything wanting two either chained prompts
   * — losing everything entered when the last one is cancelled — or built its
   * own overlay. Four call sites had gone one of those two ways, and one of
   * the hand-built ones rendered *behind* the panel that opened it, because
   * it also had to rediscover the z-index.
   *
   * @param {object}   opts
   * @param {string}   opts.title
   * @param {string}  [opts.body]
   * @param {Array}    opts.fields   [{ name, label, type, value, options,
   *                                    placeholder, hint, required }]
   *                                 type: text | password | select | checkbox
   * @param {Function} [opts.validate] (values) => message | "" — checked
   *                                 before the dialog closes, so a rule the
   *                                 server enforces can be shown here too
   *                                 rather than after the fact.
   * @returns {Promise<object|null>} the values, or null if cancelled.
   */
  function form(opts) {
    return open({ ...opts, kind: 'form' });
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
    // null for anything returning a value, so "" stays a distinguishable
    // answer from "no answer".
    if (kind === 'prompt' || kind === 'form') return null;
    return false;
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

    // An element the caller built, for when the thing being chosen is not a
    // field. The icon picker (#180) is a grid of glyphs; expressed as a
    // `select` it would be a dropdown of the words "router" and "security",
    // which is the one thing an icon picker must not be.
    if (opts.content instanceof Node) box.appendChild(opts.content);

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

    const fields = {};
    if (opts.kind === 'form') {
      (opts.fields || []).forEach((spec, index) => {
        const row = document.createElement('div');
        row.className = 'sm-dialog-field';

        const id = `sm-dialog-field-${index}`;
        const label = document.createElement('label');
        label.className = 'sm-dialog-label';
        label.htmlFor = id;
        label.textContent = spec.label || spec.name;

        let control;
        if (spec.type === 'select') {
          control = document.createElement('select');
          (spec.options || []).forEach(option => {
            const el = document.createElement('option');
            el.value = option.value !== undefined ? option.value : option;
            el.textContent = option.label !== undefined ? option.label : option;
            control.appendChild(el);
          });
        } else if (spec.type === 'checkbox') {
          control = document.createElement('input');
          control.type = 'checkbox';
          control.checked = Boolean(spec.value);
        } else if (spec.type === 'textarea') {
          // A block of text — configuration lines (#407). Monospace, because
          // indentation is meaning on every platform ShellMate knows.
          control = document.createElement('textarea');
          control.rows = spec.rows || 10;
          control.spellcheck = false;
          control.value = spec.value || '';
          control.classList.add('sm-dialog-textarea');
          if (spec.placeholder) control.placeholder = spec.placeholder;
        } else {
          control = document.createElement('input');
          control.type = spec.type === 'password' ? 'password' : 'text';
          control.autocomplete = 'off';
          control.spellcheck = false;
          if (spec.placeholder) control.placeholder = spec.placeholder;
        }

        control.id = id;
        control.className = 'sm-dialog-input';
        if (spec.type !== 'checkbox' && spec.value !== undefined) {
          control.value = spec.value;
        }
        // A field can be present but not editable — the colour picker when
        // group colours are switched off (#293). Shown rather than removed,
        // so the setting explains itself where the choice would have been.
        if (spec.disabled) {
          control.disabled = true;
          row.classList.add('sm-dialog-row-disabled');
        }
        fields[spec.name] = { control, spec };

        row.append(label, control);

        // A password cannot be read back once it is in the vault, so being
        // able to check what was typed matters more here than the masking
        // does — the alternative is discovering a typo when a device refuses
        // it a week later.
        if (spec.type === 'password') {
          const reveal = document.createElement('button');
          reveal.type = 'button';
          reveal.className = 'sm-dialog-reveal';
          reveal.textContent = 'Show';
          reveal.addEventListener('click', () => {
            const showing = control.type === 'text';
            control.type = showing ? 'password' : 'text';
            reveal.textContent = showing ? 'Show' : 'Hide';
          });
          row.appendChild(reveal);
        }

        if (spec.hint) {
          const hint = document.createElement('p');
          hint.className = 'sm-dialog-hint';
          hint.textContent = spec.hint;
          row.appendChild(hint);
        }

        box.appendChild(row);
      });
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
    /** Read every field back. */
    const collect = () => {
      const values = {};
      Object.entries(fields).forEach(([name, { control, spec }]) => {
        values[name] = spec.type === 'checkbox' ? control.checked
                                                : control.value.trim();
      });
      return values;
    };

    /** Shown in the dialog rather than after it closes. */
    const complain = (message) => {
      let el = box.querySelector('.sm-dialog-error');
      if (!el) {
        el = document.createElement('p');
        el.className = 'sm-dialog-error';
        actions.parentNode.insertBefore(el, actions);
      }
      el.textContent = message;
    };

    accept.addEventListener('click', () => {
      if (opts.kind === 'form') {
        const values = collect();

        const missing = (opts.fields || []).find(
          spec => spec.required && !values[spec.name]);
        if (missing) {
          complain(`${missing.label || missing.name} is needed.`);
          const entry = fields[missing.name];
          if (entry) entry.control.focus();
          return;
        }

        if (typeof opts.validate === 'function') {
          const problem = opts.validate(values);
          if (problem) { complain(problem); return; }
        }

        close(values);
        return;
      }
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

    // A key the dialog acts on is stopped here, not merely defaulted away.
    //
    // Every panel registers its own Escape handler on `document`, and this
    // overlay is a child of `document.body` — so without stopPropagation the
    // event carried on bubbling and cancelling a dialog *also* closed the
    // panel that opened it. Found with the history panel: Escape out of
    // "Clear history" and the whole panel went with it, which reads as the
    // clear having happened.
    //
    // preventDefault() was never going to cover this. It suppresses the
    // browser's default action, and another listener on an ancestor is not a
    // default action.
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close(cancelValue(opts.kind));
        return;
      }
      if (e.key === 'Enter' && (opts.kind !== 'confirm' || e.target !== cancel)) {
        e.preventDefault();
        e.stopPropagation();
        accept.click();
        return;
      }
      if (e.key === 'Tab') trapFocus(e, box);
    });

    // The field first where there is one, so a prompt can be answered
    // without reaching for the mouse. Otherwise the confirm button — except
    // when it is destructive, where landing on Cancel is the safer default
    // for anyone who hits Enter on reflex.
    const firstField = Object.values(fields)[0];
    const initial = input
                 || (firstField && firstField.control)
                 || (opts.danger && cancel ? cancel : accept);
    setTimeout(() => {
      initial.focus();
      if (input) input.select();
      else if (firstField && firstField.control.select) firstField.control.select();
    }, 0);
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

  window.shellmateDialog = { confirm, prompt, alert, form };
})();
