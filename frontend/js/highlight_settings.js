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
 *
 * A rule can also *alert* (#521), which is where the row grows a severity and
 * a cooldown. Colour is a reading aid for a screen somebody is looking at;
 * an alert is for the tab they are not, so the matching for it happens in the
 * backend against what the device actually said. The two switches next to the
 * tick are the ones that decide how loud it is and how often it may repeat —
 * a pattern matching a chatty `debug` with no cooldown is a rule people turn
 * off within the hour.
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

  /** Severities a watch rule may carry, loudest last (#521). */
  const SEVERITIES = ['info', 'warning', 'critical'];

  /**
   * Seconds a new watch rule waits before it may alert again.
   *
   * Matches the shipped `alerts.watch_cooldown` default. A rule written here
   * carries its own number so that changing the Stockton value later does not
   * silently re-tune rules somebody has already tuned by hand.
   */
  const DEFAULT_COOLDOWN = 60;

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
      .addEventListener('click', async () => {
        const ok = await window.shellmateDialog.confirm({
          title: 'Replace your colour rules with the defaults?',
          body: 'Every rule you have written is discarded. Nothing is saved until you press Save Settings, so this can still be abandoned by closing Settings.',
          confirmLabel: 'Replace',
          danger: true,
        });
        if (ok) render(DEFAULT_RULES);
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

    // --- The watch half of a rule (#521) -------------------------------
    //
    // Severity and cooldown only appear once the rule alerts. They are
    // meaningless otherwise, and three dead controls on every row of a
    // colour editor is how a simple list stops looking simple.
    const watch = document.createElement('div');
    watch.className = 'highlight-watch';

    const alertLabel = document.createElement('label');
    alertLabel.className = 'highlight-case';
    alertLabel.title = 'Raise an alert when this matches, on whichever tab it '
                     + 'happens — not only colour it';
    const alertBox = document.createElement('input');
    alertBox.type = 'checkbox';
    alertBox.checked = Boolean(rule.alert);
    alertLabel.appendChild(alertBox);
    alertLabel.appendChild(document.createTextNode('Alert'));

    const severity = document.createElement('select');
    severity.className = 'highlight-severity';
    severity.title = 'How loudly. Critical also sounds a tone and stays on '
                   + 'screen until dismissed.';
    [['info', 'info'], ['warning', 'warning'], ['critical', 'critical']]
      .forEach(([value, text]) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = text;
        severity.appendChild(opt);
      });
    severity.value = SEVERITIES.includes(rule.severity) ? rule.severity : 'warning';

    const cooldown = document.createElement('input');
    cooldown.type = 'number';
    cooldown.className = 'highlight-cooldown';
    cooldown.min = '0';
    cooldown.max = '3600';
    cooldown.title = 'Seconds before this rule may alert again. A flapping '
                   + 'interface matches every few seconds; without this it '
                   + 'alerts every few seconds.';
    cooldown.value = Number.isFinite(Number(rule.cooldown_s))
      ? String(rule.cooldown_s) : String(DEFAULT_COOLDOWN);

    const syncWatch = () => {
      const on = alertBox.checked;
      severity.classList.toggle('hidden', !on);
      cooldown.classList.toggle('hidden', !on);
    };
    alertBox.addEventListener('change', syncWatch);
    syncWatch();

    watch.append(alertLabel, severity, cooldown);

    // Opens the builder on this rule and writes the answer back. The inline
    // preview stays: it answers "does this match" at a glance, and the
    // builder answers "why not" — two different questions.
    const test = document.createElement('button');
    test.type = 'button';
    test.className = 'highlight-test';
    test.title = 'Build and test this pattern';
    test.innerHTML = '<span class="material-symbols-outlined">science</span>';
    test.addEventListener('click', () => {
      if (typeof window.openRegexBuilder !== 'function') return;
      window.openRegexBuilder(
        { pattern: pattern.value, colour: colour.value, ignore_case: caseBox.checked },
        (answer) => {
          pattern.value = answer.pattern;
          colour.value = answer.colour;
          caseBox.checked = answer.ignore_case;
          update();
        });
    });

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
    row.appendChild(watch);
    row.appendChild(test);
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
    return [...listEl.querySelectorAll('.highlight-rule')].map(row => {
      const cooldown = Number(row.querySelector('.highlight-cooldown').value);
      return {
        pattern:     row.querySelector('.highlight-pattern').value,
        colour:      row.querySelector('.highlight-colour').value,
        // The first .highlight-case in a row is the Aa box; the alert tick
        // shares the class for its styling and lives inside .highlight-watch.
        ignore_case: row.querySelector('.highlight-case input').checked,
        alert:       row.querySelector('.highlight-watch input').checked,
        severity:    row.querySelector('.highlight-severity').value,
        // Clamped on the way out as well as in the backend: a number input
        // accepts anything when the page is driven by a script, and the
        // backend must not be the only place this is true.
        cooldown_s:  Number.isFinite(cooldown)
          ? Math.min(Math.max(Math.round(cooldown), 0), 3600) : DEFAULT_COOLDOWN,
      };
    }).filter(rule => rule.pattern.trim());
  }

  window.highlightRulesEditor = { render, collect, DEFAULT_RULES };
})();
