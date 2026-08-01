/**
 * regex_builder.js — Build and test an output-colour pattern.
 *
 * Output Colours is the one feature whose entire value is *seeing the
 * important line*, and the regular expression is the part most likely to be
 * wrong. Without a tester the loop is: guess, save, wait for a device to print
 * the line you were aiming at, discover it did not match, guess again.
 *
 * Three things decide whether this tool is worth having rather than sending
 * somebody to a regex website:
 *
 * - **It tests against your own output.** The sample is prefilled from the
 *   active session's buffer, because that is the text somebody is actually
 *   trying to colour. A generic sample tests a generic pattern.
 * - **It uses the browser's engine**, the same one that will run the rule.
 *   JavaScript and Python differ in ways that matter here — lookbehind, named
 *   groups, what `\d` means under Unicode — and a tester using the wrong
 *   engine is worse than none, because it is confidently wrong.
 * - **The ready-made patterns are network patterns.** An interface name, a
 *   VLAN id, `line protocol is down`, CRC and input errors. That is the part
 *   a general-purpose tool cannot do for a network engineer.
 *
 * A pattern with catastrophic backtracking hangs whatever thread runs it, so
 * the match runs in a Worker that gets terminated on a deadline. Warning about
 * the possibility would not help: the tab is already frozen by then.
 */
(function () {
  'use strict';

  /** How long a test run may take before the pattern is called runaway. */
  const TEST_TIMEOUT_MS = 750;

  /** Sample lines shown when there is no session to borrow output from. */
  const FALLBACK_SAMPLE = [
    'GigabitEthernet0/1 is up, line protocol is up (connected)',
    'GigabitEthernet0/2 is down, line protocol is down (err-disabled)',
    'Vlan10 is up, line protocol is up',
    '  12 input errors, 3 CRC, 0 frame, 0 overrun, 0 ignored',
    '  Internet address is 10.20.30.1/24, MTU 1500 bytes',
    '  Hardware is Gigabit Ethernet, address is 0011.2233.4455',
    '%LINK-3-UPDOWN: Interface GigabitEthernet0/2, changed state to down',
  ].join('\n');

  /**
   * The constructs people actually reach for, as inserts rather than a wall
   * of documentation. `cursor` is where the caret lands afterwards, counted
   * back from the end of the inserted text.
   */
  const REFERENCE = [
    { insert: '\\b',      label: '\\b',    hint: 'Word boundary — stops "up" matching inside "backup"' },
    { insert: '^',        label: '^',      hint: 'Start of line' },
    { insert: '$',        label: '$',      hint: 'End of line' },
    { insert: '.',        label: '.',      hint: 'Any character' },
    { insert: '\\d',      label: '\\d',    hint: 'Any digit' },
    { insert: '\\w',      label: '\\w',    hint: 'Letter, digit or underscore' },
    { insert: '\\s',      label: '\\s',    hint: 'Any whitespace' },
    { insert: '[]',       label: '[…]',    hint: 'Any one of these characters', cursor: 1 },
    { insert: '[^]',      label: '[^…]',   hint: 'Any character except these', cursor: 1 },
    { insert: '()',       label: '(…)',    hint: 'Group, and capture what it matched', cursor: 1 },
    { insert: '(?:)',     label: '(?:…)',  hint: 'Group without capturing', cursor: 1 },
    { insert: '|',        label: '|',      hint: 'Either side — this or that' },
    { insert: '?',        label: '?',      hint: 'Optional — none or one' },
    { insert: '*',        label: '*',      hint: 'Any number, including none' },
    { insert: '+',        label: '+',      hint: 'One or more' },
    { insert: '{1,3}',    label: '{n,m}',  hint: 'Between n and m times' },
  ];

  /** Patterns for the things this tool is for. */
  const RECIPES = [
    { name: 'Interface name',
      pattern: '\\b(?:Gi|Fa|Te|Fo|Hu|Eth|Po|Vl|Lo|Tu)[a-zA-Z]*\\s?\\d+(?:/\\d+)*(?:\\.\\d+)?\\b',
      hint: 'Gi0/1, TenGigabitEthernet1/0/3, Po12, Vlan10' },
    { name: 'IPv4 address',
      pattern: '\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b',
      hint: 'Any dotted quad, with or without a prefix length after it' },
    { name: 'IPv4 with prefix',
      pattern: '\\b(?:\\d{1,3}\\.){3}\\d{1,3}/\\d{1,2}\\b',
      hint: '10.20.30.1/24' },
    { name: 'MAC address',
      pattern: '\\b(?:[0-9a-fA-F]{4}\\.){2}[0-9a-fA-F]{4}\\b|\\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\\b',
      hint: 'Both Cisco dotted-triplet and colon-separated forms' },
    { name: 'VLAN id',
      pattern: '\\bVlan\\s?(\\d{1,4})\\b',
      hint: 'Captures the number, so the rule can refer to it' },
    { name: 'Line protocol down',
      pattern: 'line protocol is (?:down|administratively down)',
      hint: 'The half of an interface line that actually matters' },
    { name: 'Interface down',
      pattern: '\\bis (?:administratively )?down\\b',
      hint: 'Distinguishes shut from failed when read with the above' },
    { name: 'Errors and drops',
      pattern: '\\b(?:[1-9]\\d*)\\s+(?:input errors|output errors|CRC|drops?|discards?|collisions|overrun|ignored)\\b',
      hint: 'Only when the counter is non-zero — zeroes are the normal case' },
    { name: 'Syslog severity 0-3',
      pattern: '%[A-Z_]+-[0-3]-[A-Z_]+',
      hint: 'Emergency through error. The ones worth seeing.' },
    { name: 'Duplex or speed mismatch',
      pattern: '\\b(?:half-duplex|duplex mismatch|speed mismatch)\\b',
      hint: 'Rare, and invisible in a wall of output until it is coloured' },
  ];

  let _worker = null;

  document.addEventListener('DOMContentLoaded', () => {
    // The section-level entry point, for somebody building a pattern before
    // deciding which rule it belongs to.
    const open = document.getElementById('highlight-open-builder');
    if (open) open.addEventListener('click', () => openBuilder());
  });

  /**
   * Open the builder.
   *
   * @param {Object}   [rule]     Existing pattern/colour/ignore_case to load.
   * @param {Function} [onAccept] Called with the finished rule when accepted.
   */
  function openBuilder(rule, onAccept) {
    rule = rule || {};

    const overlay = document.createElement('div');
    // Above .panel-overlay (200), because this is opened from inside a panel.
    // The connection dialog's #modal-overlay is 100 and would render
    // underneath the very panel that opened it — a defect this interface has
    // already had once.
    overlay.className = 'regex-overlay';

    const box = document.createElement('div');
    box.className = 'regex-box settings-like';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', 'Pattern builder');

    box.appendChild(_header(() => close(null)));

    const body = document.createElement('div');
    body.className = 'regex-body';

    // --- the pattern ------------------------------------------------------
    const patternSection = _section('Pattern');
    const patternInput = document.createElement('input');
    patternInput.type = 'text';
    patternInput.className = 'regex-pattern';
    patternInput.spellcheck = false;
    patternInput.autocomplete = 'off';
    patternInput.placeholder = 'line protocol is down';
    patternInput.value = rule.pattern || '';

    const options = document.createElement('div');
    options.className = 'regex-options';

    const caseLabel = document.createElement('label');
    const caseBox = document.createElement('input');
    caseBox.type = 'checkbox';
    caseBox.checked = rule.ignore_case !== false;
    caseLabel.append(caseBox, document.createTextNode(' Ignore case'));

    const colourLabel = document.createElement('label');
    const colourSelect = document.createElement('select');
    colourSelect.className = 'regex-colour';
    const colours = window.shellmateHighlight
      ? window.shellmateHighlight.availableColours()
      : ['red', 'orange', 'yellow', 'green'];
    colours.forEach(name => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      colourSelect.appendChild(option);
    });
    colourSelect.value = rule.colour || 'yellow';
    colourLabel.append(document.createTextNode('Colour '), colourSelect);

    options.append(caseLabel, colourLabel);

    const status = document.createElement('div');
    status.className = 'regex-status';

    patternSection.append(patternInput, options, status);
    body.appendChild(patternSection);

    // --- reference --------------------------------------------------------
    const referenceSection = _section('Building blocks');
    const chips = document.createElement('div');
    chips.className = 'regex-chips';
    REFERENCE.forEach(item => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'regex-chip';
      chip.textContent = item.label;
      chip.title = item.hint;
      chip.addEventListener('click', () => {
        _insert(patternInput, item.insert, item.cursor || 0);
        run();
      });
      chips.appendChild(chip);
    });
    referenceSection.appendChild(chips);
    body.appendChild(referenceSection);

    // --- ready-made -------------------------------------------------------
    const recipeSection = _section('Ready-made');
    const recipeList = document.createElement('div');
    recipeList.className = 'regex-recipes';
    RECIPES.forEach(recipe => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'regex-recipe';

      const name = document.createElement('span');
      name.className = 'regex-recipe-name';
      name.textContent = recipe.name;

      const hint = document.createElement('span');
      hint.className = 'regex-recipe-hint';
      hint.textContent = recipe.hint;

      item.append(name, hint);
      item.addEventListener('click', () => {
        patternInput.value = recipe.pattern;
        patternInput.focus();
        run();
      });
      recipeList.appendChild(item);
    });
    recipeSection.appendChild(recipeList);
    body.appendChild(recipeSection);

    // --- sample and result ------------------------------------------------
    const sampleSection = _section('Test against');
    const sampleNote = document.createElement('p');
    sampleNote.className = 'regex-note';

    const sample = document.createElement('textarea');
    sample.className = 'regex-sample';
    sample.spellcheck = false;
    sample.rows = 8;

    const borrowed = _recentOutput();
    sample.value = borrowed || FALLBACK_SAMPLE;
    sampleNote.textContent = borrowed
      ? 'The last lines from the active session — the output you are actually trying to colour. Edit it freely; nothing here is sent anywhere.'
      : 'No session is open, so this is sample interface output. Connect to a device and reopen this to test against real output.';

    const result = document.createElement('div');
    result.className = 'regex-result';

    const groups = document.createElement('div');
    groups.className = 'regex-groups';

    sampleSection.append(sampleNote, sample, result, groups);
    body.appendChild(sampleSection);

    box.appendChild(body);

    // --- actions ----------------------------------------------------------
    const actions = document.createElement('div');
    actions.className = 'regex-actions';

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn-tertiary';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => close(null));

    const accept = document.createElement('button');
    accept.type = 'button';
    accept.className = 'btn-primary';
    accept.textContent = onAccept ? 'Use this pattern' : 'Copy pattern';
    accept.addEventListener('click', () => {
      const value = {
        pattern: patternInput.value,
        colour: colourSelect.value,
        ignore_case: caseBox.checked,
      };
      if (onAccept) { close(value); return; }
      navigator.clipboard.writeText(value.pattern).catch(() => {});
      close(null);
    });

    actions.append(cancel, accept);
    box.appendChild(actions);

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // Escape is stopped here rather than only defaulted away: every panel has
    // a document-level Escape handler, and this overlay is a child of body,
    // so without stopPropagation cancelling would also close Settings behind
    // it. Exactly the bug the shared dialog had.
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close(null);
      }
    });
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) close(null);
    });

    let debounce = null;
    const schedule = () => {
      clearTimeout(debounce);
      debounce = setTimeout(run, 150);
    };
    patternInput.addEventListener('input', schedule);
    sample.addEventListener('input', schedule);
    caseBox.addEventListener('change', run);
    colourSelect.addEventListener('change', run);

    patternInput.focus();
    run();

    function run() {
      _test(patternInput.value, sample.value, caseBox.checked, colourSelect.value,
            status, result, groups, accept);
    }

    function close(value) {
      clearTimeout(debounce);
      _killWorker();
      overlay.remove();
      if (value && onAccept) onAccept(value);
    }
  }

  // -------------------------------------------------------------------------
  // Running the pattern
  // -------------------------------------------------------------------------

  /**
   * Match in a Worker, with a deadline.
   *
   * A pattern like `(a+)+b` against a long line backtracks for longer than
   * anybody will wait, and a regex cannot be interrupted on the thread running
   * it. So it runs somewhere terminable. Doing this on the main thread would
   * freeze the tab — including the terminals — which is a far worse outcome
   * than an unhelpful tester.
   */
  function _test(pattern, text, ignoreCase, colour, status, result, groups, accept) {
    _killWorker();
    result.innerHTML = '';
    groups.innerHTML = '';

    if (!pattern) {
      status.className = 'regex-status';
      status.textContent = 'Type a pattern, pick a ready-made one, or build it up from the blocks above.';
      accept.disabled = true;
      return;
    }

    // Compiled here as well as in the worker, because a syntax error should be
    // reported instantly and named — that is the message people need.
    try {
      new RegExp(pattern, ignoreCase ? 'gi' : 'g');
    } catch (e) {
      status.className = 'regex-status regex-status-error';
      status.textContent = `Not a valid pattern: ${e.message}`;
      accept.disabled = true;
      return;
    }

    accept.disabled = false;
    status.className = 'regex-status regex-status-working';
    status.textContent = 'Testing…';

    const source = `
      onmessage = (e) => {
        const { pattern, flags, text } = e.data;
        const re = new RegExp(pattern, flags);
        const hits = [];
        let m;
        while ((m = re.exec(text)) !== null) {
          hits.push({ index: m.index, text: m[0], groups: m.slice(1) });
          if (m[0] === '') re.lastIndex++;
          if (hits.length > 5000) break;
        }
        postMessage(hits);
      };
    `;
    const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));

    let settled = false;
    const deadline = setTimeout(() => {
      if (settled) return;
      settled = true;
      _killWorker();
      status.className = 'regex-status regex-status-error';
      status.textContent =
        'This pattern took too long to run and was stopped. Nested repeats '
        + 'like (a+)+ can take longer than the age of the universe on the '
        + 'wrong input — and it would have frozen the terminal, not just this '
        + 'box.';
      accept.disabled = true;
    }, TEST_TIMEOUT_MS);

    try {
      _worker = new Worker(url);
    } catch (e) {
      // No Worker available: fall back to the main thread rather than losing
      // the feature. The deadline cannot save the tab here, which is why this
      // is the fallback and not the implementation.
      clearTimeout(deadline);
      URL.revokeObjectURL(url);
      _renderMatches(_matchHere(pattern, text, ignoreCase), text, colour,
                     status, result, groups);
      return;
    }

    _worker.onmessage = (e) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      URL.revokeObjectURL(url);
      _killWorker();
      _renderMatches(e.data, text, colour, status, result, groups);
    };
    _worker.onerror = () => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      _killWorker();
      status.className = 'regex-status regex-status-error';
      status.textContent = 'The pattern could not be run.';
      accept.disabled = true;
    };
    _worker.postMessage({ pattern, flags: ignoreCase ? 'gi' : 'g', text });
  }

  function _matchHere(pattern, text, ignoreCase) {
    const re = new RegExp(pattern, ignoreCase ? 'gi' : 'g');
    const hits = [];
    let m;
    while ((m = re.exec(text)) !== null) {
      hits.push({ index: m.index, text: m[0], groups: m.slice(1) });
      if (m[0] === '') re.lastIndex++;
      if (hits.length > 5000) break;
    }
    return hits;
  }

  /** Paint the sample with the matches, in the colour the rule will use. */
  function _renderMatches(hits, text, colour, status, result, groups) {
    result.innerHTML = '';
    groups.innerHTML = '';

    if (!hits.length) {
      status.className = 'regex-status regex-status-nomatch';
      status.textContent = 'Valid, but it matches nothing in the sample.';
    } else {
      status.className = 'regex-status regex-status-ok';
      status.textContent =
        `${hits.length} match${hits.length === 1 ? '' : 'es'}, shown below in the rule's colour.`;
    }

    // Built from the match offsets rather than by re-running the pattern, so
    // what is highlighted is exactly what the worker found.
    let cursor = 0;
    hits.forEach(hit => {
      if (hit.index > cursor) {
        result.appendChild(document.createTextNode(text.slice(cursor, hit.index)));
      }
      const span = document.createElement('span');
      span.className = `hl-${colour}`;
      span.textContent = hit.text;
      result.appendChild(span);
      cursor = hit.index + hit.text.length;
    });
    if (cursor < text.length) {
      result.appendChild(document.createTextNode(text.slice(cursor)));
    }

    // Capture groups, from the first match that has any. Rules may refer to
    // them, and "did my group capture what I meant" is the second question
    // after "did it match at all".
    const withGroups = hits.find(h => h.groups && h.groups.length);
    if (!withGroups) return;

    const heading = document.createElement('div');
    heading.className = 'regex-groups-heading';
    heading.textContent = `Capture groups, from the first match ("${withGroups.text}")`;
    groups.appendChild(heading);

    withGroups.groups.forEach((value, i) => {
      const row = document.createElement('div');
      row.className = 'regex-group';

      const number = document.createElement('span');
      number.className = 'regex-group-number';
      number.textContent = `\\${i + 1}`;

      const captured = document.createElement('span');
      captured.className = 'regex-group-value';
      // undefined means the group is in a branch that did not participate,
      // which is different from having matched an empty string.
      captured.textContent = value === undefined ? '(did not participate)'
                           : value === ''        ? '(empty)'
                           : value;
      if (value === undefined || value === '') captured.classList.add('regex-group-empty');

      row.append(number, captured);
      groups.appendChild(row);
    });
  }

  function _killWorker() {
    if (_worker) {
      try { _worker.terminate(); } catch (_) {}
      _worker = null;
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  /** The active session's recent output, or '' when there is no session. */
  function _recentOutput() {
    try {
      const tab = window.getActiveTab && window.getActiveTab();
      if (!tab || typeof tab.getBufferLines !== 'function') return '';
      const lines = tab.getBufferLines(40) || [];
      // Trailing blank lines are most of a terminal screen and make the box
      // look empty when it is not.
      while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
      return lines.join('\n').trim();
    } catch (_) {
      return '';
    }
  }

  function _insert(input, text, back) {
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    const caret = start + text.length - back;
    input.focus();
    input.setSelectionRange(caret, caret);
  }

  function _section(title) {
    const section = document.createElement('section');
    section.className = 'settings-section';
    const heading = document.createElement('h3');
    heading.className = 'settings-section-title';
    heading.textContent = title;
    section.appendChild(heading);
    return section;
  }

  function _header(onClose) {
    const header = document.createElement('div');
    header.className = 'panel-header';

    const title = document.createElement('h2');
    title.className = 'panel-title';
    title.textContent = 'Pattern builder';

    const close = document.createElement('button');
    close.className = 'icon-btn';
    close.setAttribute('aria-label', 'Close');
    close.innerHTML = '<span class="material-symbols-outlined">close</span>';
    close.addEventListener('click', onClose);

    header.append(title, close);
    return header;
  }

  window.openRegexBuilder = openBuilder;
})();
