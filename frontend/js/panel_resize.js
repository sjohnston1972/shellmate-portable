/**
 * panel_resize.js — Dragging a side panel wider.
 *
 * Every side panel is a fixed 420px, or 620 for the wide ones. History is the
 * one that suffers most: search results are command lines and their output,
 * which are long, and a wrapped `show running-config` line is considerably
 * harder to scan than a truncated one.
 *
 * The chat divider already does this and persists its position, so this is the
 * same idea applied to the panels rather than a second mechanism — the width
 * goes to settings.json for the same reason the chat fraction does, so a
 * preference travels with the data folder rather than living in one browser.
 *
 * Applied to every `.side-panel` rather than to History alone. Files and
 * Broadcast have the same problem to a lesser degree, and a handle that
 * appears on one panel and not another is a worse answer than one that is
 * simply there.
 */
(function () {
  'use strict';

  /** Below this a panel stops being usable; above it, it swallows the screen. */
  const MIN_WIDTH = 320;
  const MAX_FRACTION = 0.9;

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.side-panel').forEach(attach);
    applyStoredWidths();
    window.addEventListener('shellmate:settings-loaded', applyStoredWidths);
  });

  function storedWidths() {
    return ((window.shellmateSettings || {}).interface || {}).panel_widths || {};
  }

  function applyStoredWidths() {
    const widths = storedWidths();
    Object.entries(widths).forEach(([id, width]) => {
      const panel = document.getElementById(id);
      if (panel && Number(width) >= MIN_WIDTH) {
        panel.style.width = `${Math.round(Number(width))}px`;
      }
    });
  }

  function attach(panel) {
    if (!panel.id) return;

    const handle = document.createElement('div');
    handle.className = 'panel-resize-handle';
    handle.title = 'Drag to resize';
    panel.appendChild(handle);

    let startX = 0;
    let startWidth = 0;

    const onMove = (e) => {
      // Panels are anchored right, so dragging left makes them wider.
      const width = Math.min(
        Math.max(startWidth + (startX - e.clientX), MIN_WIDTH),
        window.innerWidth * MAX_FRACTION);
      panel.style.width = `${Math.round(width)}px`;
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.classList.remove('resizing-panel');
      remember(panel.id, panel.getBoundingClientRect().width);
    };

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      startX = e.clientX;
      startWidth = panel.getBoundingClientRect().width;
      document.body.classList.add('resizing-panel');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Double-clicking the handle goes back to the stylesheet's width, which
    // is the way out of having dragged it somewhere useless.
    handle.addEventListener('dblclick', () => {
      panel.style.width = '';
      remember(panel.id, 0);
    });
  }

  /**
   * Persist the width.
   *
   * Written on mouse-up rather than during the drag: one save per resize
   * rather than one per frame.
   */
  function remember(id, width) {
    const widths = { ...storedWidths() };
    if (width >= MIN_WIDTH) widths[id] = Math.round(width);
    else delete widths[id];

    fetch('/api/settings', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      // The endpoint takes a { settings: … } envelope; posting the block
      // directly is accepted and silently stores nothing.
      body:    JSON.stringify({ settings: { interface: { panel_widths: widths } } }),
    }).then(() => {
      if (window.shellmateSettings && window.shellmateSettings.interface) {
        window.shellmateSettings.interface.panel_widths = widths;
      }
    }).catch(() => { /* the width still applies for this session */ });
  }
})();
