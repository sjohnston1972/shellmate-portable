/**
 * usage.js — ShellMate's own footprint, in the status bar (#266).
 *
 * The application's process, not the machine: with five thousand profiles
 * and a dozen sessions the question "is ShellMate the thing eating this
 * laptop" deserves an answer on screen rather than a trip to Task Manager.
 *
 * Polled gently — a footprint readout that measurably grows the footprint
 * would be its own punchline. The first reading carries memory only; CPU
 * needs two samples to mean anything, and the server says so by sending
 * null until it has them.
 */
(function () {
  'use strict';

  const EVERY_MS = 5000;

  let wrap, el;

  document.addEventListener('DOMContentLoaded', () => {
    wrap = document.getElementById('status-usage-wrap');
    el = document.getElementById('status-usage');
    if (!wrap || !el) return;
    tick();
    // Paused while the window is hidden (#491): a footprint readout nobody
    // can see was fetching itself every five seconds behind a closed window.
    if (window.shellmateVisibility) window.shellmateVisibility.every(tick, EVERY_MS);
    else setInterval(tick, EVERY_MS);
  });

  async function tick() {
    try {
      const res = await fetch('/api/system/stats', { cache: 'no-store' });
      if (!res.ok) throw new Error();
      const stats = await res.json();

      const bits = [];
      if (stats.cpu_percent !== null && stats.cpu_percent !== undefined) {
        bits.push(`CPU ${stats.cpu_percent.toFixed(0)}%`);
      }
      if (stats.memory_mb) {
        bits.push(`${stats.memory_mb.toFixed(0)} MB`);
      }

      el.textContent = bits.join(' · ');
      wrap.classList.toggle('hidden', !bits.length);
    } catch (_) {
      // The reading is a nicety; a failed fetch hides it rather than
      // decorating the status bar with an error.
      wrap.classList.add('hidden');
    }
  }
})();
