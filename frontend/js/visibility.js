/**
 * visibility.js — Timers that stop while the window is hidden (#491).
 *
 * Closing the desktop window only hides it: the sessions live in the server
 * and must survive the window going away. The page, though, kept every poll
 * and every per-second redraw running behind the hidden window forever — a
 * "closed" ShellMate fetched its own footprint every five seconds and
 * re-rendered a status bar nobody could see.
 *
 * Anything periodic goes through `every()` here rather than setInterval. It
 * runs while the page is visible, stops when it is hidden, and on return
 * runs once straight away so a stale reading is replaced at once rather than
 * after a full interval. Loaded before every module that ticks.
 */
(function () {
  'use strict';

  /** Every registered timer, so one visibilitychange can reach them all. */
  const timers = new Set();

  /**
   * A setInterval that follows the page's visibility.
   *
   * @param {Function} fn    What to run.
   * @param {number}   ms    How often, while visible.
   * @param {object}  [opts]
   * @param {boolean} [opts.immediate=true]  Run once on becoming visible
   *   again, before the interval resumes.
   * @returns {{stop: Function, start: Function, running: boolean}}
   */
  function every(fn, ms, opts) {
    const t = {
      fn, ms,
      handle: null,
      immediate: !(opts && opts.immediate === false),
    };
    const run = () => { try { t.fn(); } catch (err) { console.warn('Periodic task failed', err); } };
    const begin = () => { if (t.handle === null) t.handle = setInterval(run, t.ms); };
    const end = () => {
      if (t.handle !== null) { clearInterval(t.handle); t.handle = null; }
    };
    t.begin = begin;
    t.end = end;
    t.run = run;

    timers.add(t);
    if (!document.hidden) begin();

    return {
      /** Stop for good — the timer is forgotten, not merely paused. */
      stop: () => { end(); timers.delete(t); },
      /** Register again after stop(); ticks only once the page is visible. */
      start: () => { timers.add(t); if (!document.hidden) begin(); },
      get running() { return t.handle !== null; },
    };
  }

  document.addEventListener('visibilitychange', () => {
    timers.forEach(t => {
      if (document.hidden) { t.end(); return; }
      if (t.immediate) t.run();
      t.begin();
    });
  });

  window.shellmateVisibility = {
    every,
    /** Whether the page is hidden right now. */
    hidden: () => Boolean(document.hidden),
  };
})();
