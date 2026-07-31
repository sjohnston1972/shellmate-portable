/**
 * highlight.js — Colour terminal output by regex rule.
 *
 * "down" in red, "up" in green, "error" in orange — scanning a wall of
 * interface output for the one line that matters is most of what troubleshooting
 * actually looks like, and colour does that far faster than reading does.
 *
 * Implemented by injecting SGR escape sequences into the stream before it
 * reaches xterm, which is the terminal's own colour mechanism rather than a
 * layer bolted over it. That means it survives scrollback, selection and copy
 * exactly as device-sent colour does.
 *
 * The one thing that must not happen is corrupting what the device already
 * sent. Escape sequences are split out first and passed through untouched, so
 * rules only ever apply to plain text — a regex running over raw stream bytes
 * would happily match inside `\x1b[32m` and produce a terminal full of
 * garbage.
 */
(function () {
  'use strict';

  /**
   * Named colours mapped to SGR codes.
   *
   * 256-colour codes rather than the 8 basic ones: the basic set is remapped
   * by whichever theme is active, so "red" could arrive as something else
   * entirely. These stay put.
   */
  const COLOURS = {
    red:     '38;5;203',
    orange:  '38;5;215',
    yellow:  '38;5;221',
    green:   '38;5;114',
    blue:    '38;5;111',
    cyan:    '38;5;116',
    magenta: '38;5;176',
    grey:    '38;5;245',
    white:   '38;5;255',
  };

  const RESET = '\x1b[0m';

  /** Matches any escape sequence, so it can be stepped over. */
  const ESCAPE_RE = /\x1b(?:\[[0-?]*[ -\/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])/g;

  let compiled = [];
  let enabled = true;

  /**
   * Rebuild the rule set from settings.
   *
   * A bad regex from the settings panel is skipped rather than allowed to
   * throw on every chunk of output for the rest of the session.
   */
  function setRules(config) {
    enabled = Boolean(config && config.enabled);
    compiled = [];

    if (!enabled || !config.rules) return;

    config.rules.forEach(rule => {
      if (!rule || !rule.pattern) return;
      const sgr = COLOURS[rule.colour] || COLOURS.yellow;
      try {
        compiled.push({
          re: new RegExp(rule.pattern, rule.ignore_case === false ? 'g' : 'gi'),
          open: `\x1b[${sgr}m`,
        });
      } catch (e) {
        console.warn('Ignoring invalid highlight pattern:', rule.pattern, e.message);
      }
    });
  }

  /**
   * Apply the rules to a chunk of terminal output.
   *
   * Returns the chunk unchanged when highlighting is off or nothing matches,
   * so the common case costs one regex test rather than a rebuild.
   */
  function apply(text) {
    if (!enabled || !compiled.length || !text) return text;

    let result = '';
    let index = 0;

    // Walk the chunk, alternating between escape sequences (passed through
    // verbatim) and plain text (where rules apply).
    ESCAPE_RE.lastIndex = 0;
    let match;
    while ((match = ESCAPE_RE.exec(text)) !== null) {
      result += colourise(text.slice(index, match.index));
      result += match[0];
      index = match.index + match[0].length;
    }
    result += colourise(text.slice(index));

    return result;
  }

  function colourise(segment) {
    if (!segment) return segment;

    for (const rule of compiled) {
      rule.re.lastIndex = 0;
      if (!rule.re.test(segment)) continue;
      rule.re.lastIndex = 0;
      segment = segment.replace(rule.re, (m) => rule.open + m + RESET);
    }
    return segment;
  }

  /** Colours the settings panel can offer. */
  function availableColours() {
    return Object.keys(COLOURS);
  }

  window.shellmateHighlight = { setRules, apply, availableColours };
})();
