/**
 * highlight_settings.js — Editor for the output colour rules.
 *
 * Kept apart from settings.js because the rule list is a repeating editable
 * row rather than a fixed field, and mixing the two would leave settings.js
 * juggling DOM construction alongside its flat form handling.
 *
 * Each rule shows its own live preview. A regex that does not do what someone
 * expected is the normal outcome of writing one, and seeing it match — or not
 * — while typing is far more useful than saving and hunting for a line of
 * output that should have gone red.
 */
(function () {
  'use strict';

  const DEFAULT_RULES = [
    { pattern: '\\b(down|err-disabled|failed|failure|denied|unreachable)\\b',
      colour: 'red', ignore_case: true },
    { pattern: '\\b(error|errors|CRC|drop|drops|discard|discards)\\b',
      colour: 'orange', ignore_case: true },
    { pattern: '\\b(up|connected|established|active|success|ok)\\b',
      colour: 'green', ignore_case: true },
    { pattern: '\\b(warning|notice|shutdown|disabled)\\b',
      colour: 'yellow', ignore_case: true },
  ];

  const PREVIEW_TEXT =
    'GigabitEthernet0/1 is up, line protocol is up (connected)\n' +
    'GigabitEthernet0/2 is down, line protocol is down (err-disabled)\n' +
    '  12 input errors, 3 CRC, 0 drops — warning: threshold exceeded';

  let listEl;

  document.addEventListener('DOMContentLoaded', () => {
    listEl = document.getElementById('highlight-rules');
    if (!listEl) return;

    document.getElementById('highlight-add-rule')
      .addEventListener('click', () => {
        addRow({ pattern: '', colour: 'yellow', ignore_case: true });
      });

    document.getElementById('highlight-reset')
      .addEventListener('click', () => {
        if (window.confirm('Replace your colour rules with the defaults?')) {
          render(DEFAULT_RULES);
        }
      });
  });

  /** Rebuild the editor from a rule list. */
  function render(rules) {
    if (!listEl) return;
    listEl.innerHTML = '';
    (rules || []).forEach(addRow);
  }

  function addRow(rule) {
    const row = document.createElement('div');
    row.className = 'highlight-rule';

    const pattern = document.createElement('input');
    pattern.type = 'text';
    pattern.className = 'highlight-pattern';
    pattern.placeholder = 'regular expression';
    pattern.spellcheck = false;
    pattern.value = rule.pattern || '';

    const colour = document.createElement('select');
    colour.className = 'highlight-colour';
    const colours = window.shellmateHighlight
      ? window.shellmateHighlight.availableColours()
      : ['red', 'orange', 'yellow', 'green'];
    colours.forEach(name => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      colour.appendChild(opt);
    });
    colour.value = rule.colour || 'yellow';

    const caseLabel = document.createElement('label');
    caseLabel.className = 'highlight-case';
    caseLabel.title = 'Ignore case';
    const caseBox = document.createElement('input');
    caseBox.type = 'checkbox';
    caseBox.checked = rule.ignore_case !== false;
    caseLabel.appendChild(caseBox);
    caseLabel.appendChild(document.createTextNode('Aa'));

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'highlight-remove';
    remove.title = 'Remove rule';
    remove.innerHTML = '<span class="material-symbols-outlined">close</span>';
    remove.addEventListener('click', () => row.remove());

    const preview = document.createElement('div');
    preview.className = 'highlight-preview';

    const update = () => renderPreview(preview, pattern.value, colour.value, caseBox.checked);
    pattern.addEventListener('input', update);
    colour.addEventListener('change', update);
    caseBox.addEventListener('change', update);

    row.appendChild(pattern);
    row.appendChild(colour);
    row.appendChild(caseLabel);
    row.appendChild(remove);
    row.appendChild(preview);
    listEl.appendChild(row);

    update();
  }

  /**
   * Show what the rule matches against a sample of interface output.
   *
   * An invalid regex is reported here rather than silently doing nothing,
   * which is what would otherwise happen once it reached the highlighter.
   */
  function renderPreview(target, pattern, colour, ignoreCase) {
    target.innerHTML = '';

    if (!pattern) {
      target.textContent = '';
      return;
    }

    let re;
    try {
      re = new RegExp(pattern, ignoreCase ? 'gi' : 'g');
    } catch (e) {
      target.textContent = `Invalid pattern: ${e.message}`;
      target.classList.add('highlight-preview-error');
      return;
    }
    target.classList.remove('highlight-preview-error');

    let matched = false;
    PREVIEW_TEXT.split('\n').forEach(line => {
      const lineEl = document.createElement('div');
      let last = 0;
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(line)) !== null) {
        if (m.index > last) lineEl.appendChild(document.createTextNode(line.slice(last, m.index)));
        const hit = document.createElement('span');
        hit.className = `hl-${colour}`;
        hit.textContent = m[0];
        lineEl.appendChild(hit);
        last = m.index + m[0].length;
        matched = true;
        if (m[0] === '') re.lastIndex++;      // guard against a zero-width loop
      }
      if (last < line.length) lineEl.appendChild(document.createTextNode(line.slice(last)));
      target.appendChild(lineEl);
    });

    if (!matched) {
      const note = document.createElement('div');
      note.className = 'highlight-nomatch';
      note.textContent = 'No match in the sample output.';
      target.appendChild(note);
    }
  }

  /** Read the editor back out as a rule list, for saving. */
  function collect() {
    if (!listEl) return [];
    return [...listEl.querySelectorAll('.highlight-rule')].map(row => ({
      pattern:     row.querySelector('.highlight-pattern').value,
      colour:      row.querySelector('.highlight-colour').value,
      ignore_case: row.querySelector('.highlight-case input').checked,
    })).filter(rule => rule.pattern.trim());
  }

  window.highlightRulesEditor = { render, collect, DEFAULT_RULES };
})();
