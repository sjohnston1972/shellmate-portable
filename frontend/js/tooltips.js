/**
 * tooltips.js — The explanation that does not fit on the label.
 *
 * Settings rows carry a label and, sometimes, a hint paragraph under the
 * section. Between them they cover most things, and a tooltip on every row
 * would be noise — so this is deliberately opt-in: a row gets one only where
 * the label alone is ambiguous, or where the consequence of the setting is not
 * obvious from its name. "Copy on Select" says what it does and nothing about
 * what it takes away from you; "Scrollback Lines" says nothing about the cost
 * of a large one. Those are the cases.
 *
 * Marked up as `data-tip` on the row's label, and rendered as a small info
 * badge beside it. Three things it is not:
 *
 *   - Not `title=`. That is a tooltip you cannot reach from the keyboard, that
 *     takes a second to appear, and that renders as a single unstyled line.
 *   - Not innerHTML. The text is written by us, but this reads attributes out
 *     of the DOM and one day something else will write one, so it builds nodes
 *     instead. Backticks become <code>, which is all the richness needed.
 *   - Not hover-only. The badge is a real button: click, Enter and Space all
 *     open it, Escape closes it.
 */
(function () {
  'use strict';

  let bubble = null;
  let anchor = null;

  document.addEventListener('DOMContentLoaded', () => {
    decorate(document);

    // Sections are built from static markup, but the platform editor and the
    // colour-rule editor inject rows later.
    //
    // Scoped to the two containers that hold tips rather than to <body>:
    // xterm.js rewrites the terminal's DOM on every line of output, and a
    // whole-document observer would re-scan on each one.
    ['settings-panel', 'modal-dialog'].forEach(id => {
      const host = document.getElementById(id);
      if (host) {
        new MutationObserver(() => decorate(host))
          .observe(host, { childList: true, subtree: true });
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') hide();
    });
    // Any scroll moves the anchor out from under a fixed bubble.
    document.addEventListener('scroll', hide, true);
  });

  /** Give every [data-tip] element a badge, once. */
  function decorate(root) {
    root.querySelectorAll('[data-tip]:not([data-tip-ready])').forEach(el => {
      el.setAttribute('data-tip-ready', '');

      const badge = document.createElement('button');
      badge.type = 'button';
      badge.className = 'tip-badge';
      badge.setAttribute('aria-label', 'What does this setting do?');
      badge.setAttribute('aria-expanded', 'false');

      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.textContent = 'help';
      badge.appendChild(icon);

      badge.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (anchor === badge) { hide(); return; }
        show(badge, el.getAttribute('data-tip'));
      });
      badge.addEventListener('mouseenter', () => show(badge, el.getAttribute('data-tip')));
      badge.addEventListener('mouseleave', () => { if (!badge.matches(':focus')) hide(); });
      badge.addEventListener('focus', () => show(badge, el.getAttribute('data-tip')));
      badge.addEventListener('blur', hide);

      // A <label> would forward a click on the badge to its control, which
      // for a checkbox row means asking what a setting does and toggling it.
      if (el.tagName === 'LABEL') {
        badge.addEventListener('mousedown', (e) => e.preventDefault());
      }

      el.appendChild(badge);
    });
  }

  function show(badge, text) {
    if (!text) return;
    hide();

    bubble = document.createElement('div');
    bubble.className = 'tip-bubble';
    bubble.setAttribute('role', 'tooltip');
    bubble.id = 'tip-bubble';

    // Blank line = paragraph, `backticks` = code. Built as nodes, never parsed
    // as markup.
    text.split(/\n{2,}|\|\|/).forEach(paragraph => {
      const p = document.createElement('p');
      paragraph.split(/`([^`]+)`/).forEach((part, index) => {
        if (index % 2) {
          const code = document.createElement('code');
          code.textContent = part;
          p.appendChild(code);
        } else if (part) {
          p.appendChild(document.createTextNode(part));
        }
      });
      bubble.appendChild(p);
    });

    document.body.appendChild(bubble);
    position(badge);

    anchor = badge;
    badge.setAttribute('aria-expanded', 'true');
    badge.setAttribute('aria-describedby', bubble.id);
  }

  /**
   * Put the bubble where it fits.
   *
   * Settings live in a panel pinned to the right edge, so a bubble that only
   * ever opened rightwards would spend most of its life off-screen.
   */
  function position(badge) {
    const box = badge.getBoundingClientRect();
    const size = bubble.getBoundingClientRect();
    const margin = 8;

    let left = box.right + margin;
    if (left + size.width > window.innerWidth - margin) {
      left = box.left - size.width - margin;
    }
    left = Math.max(margin, Math.min(left, window.innerWidth - size.width - margin));

    let top = box.top - 4;
    if (top + size.height > window.innerHeight - margin) {
      top = window.innerHeight - size.height - margin;
    }
    top = Math.max(margin, top);

    bubble.style.left = `${left}px`;
    bubble.style.top = `${top}px`;
  }

  function hide() {
    if (bubble) bubble.remove();
    bubble = null;
    if (anchor) {
      anchor.setAttribute('aria-expanded', 'false');
      anchor.removeAttribute('aria-describedby');
    }
    anchor = null;
  }
})();
