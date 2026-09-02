/**
 * menu.js — The one context menu.
 *
 * Six places built `.tab-context-menu` by hand — the tab strip, the tab bar
 * background, the special-commands submenu, and five in the group tree —
 * each with its own positioning, its own clamp and its own idea of how a
 * menu is dismissed (#425). They had already drifted: the tab menu's Escape
 * listener was registered with `{ once: true }`, so any *other* first
 * keystroke consumed it and Escape then did nothing until the menu was
 * clicked away (#429). A rule that lives in one place cannot drift.
 *
 * Two entry points, because the menus are of two kinds:
 *
 *   shellmateMenu.open(event, items, opts)   builds the menu from a list
 *   shellmateMenu.attach(element, event)     adopts one built elsewhere
 *
 * `attach` exists for the tab menu, which is data-driven from its own table
 * and grows submenus after opening. Rebuilding it here would have moved a
 * thousand lines for no change in behaviour; adopting it gives it the same
 * positioning, dismissal and keyboard model as everything else.
 *
 * Dismissal is a click anywhere outside, a right-click anywhere outside,
 * Escape, the window resizing, or another menu opening. Listeners are added
 * on open and removed on close — never `once`, which is how #429 happened.
 *
 * Keyboard: the menu takes focus when it opens (so Escape and the arrows
 * work without a mouse), Up/Down move between enabled entries, Home/End
 * jump, Enter or Space activates, and focus goes back where it was when the
 * menu closes. It carries `role="menu"` and its entries `role="menuitem"`,
 * which is what a screen reader needs to announce it as a menu at all.
 */
(function () {
  'use strict';

  let current = null;         // the open menu element, or null
  let restoreFocus = null;    // what had focus before it opened

  /**
   * Build and show a menu.
   *
   * @param {MouseEvent|{clientX,clientY}|Element} at
   *        Where to put it: an event (or anything with clientX/clientY)
   *        opens at the pointer; an element opens beneath that element.
   * @param {Array} items  Each one of:
   *        - 'sep'                       a separator line
   *        - { heading: 'Text' }         a non-interactive heading
   *        - { icon, label, onClick, danger, disabled, title, value }
   *          icon is a Material Symbols name (optional); `value` is a
   *          dimmed suffix shown after the label; `title` is the tooltip,
   *          and for a disabled entry the reason it is disabled.
   * @param {object} [opts]
   * @param {string} [opts.className]  Extra class on the menu element.
   * @returns {HTMLElement} the menu, for callers that append to it.
   */
  function open(at, items, opts) {
    const menu = document.createElement('div');
    menu.className = 'tab-context-menu' + (opts && opts.className ? ' ' + opts.className : '');

    (items || []).forEach(item => {
      if (item === 'sep') {
        const sep = document.createElement('div');
        sep.className = 'ctx-sep';
        sep.setAttribute('role', 'separator');
        menu.appendChild(sep);
        return;
      }
      if (!item) return;
      if (item.heading) {
        const heading = document.createElement('div');
        heading.className = 'ctx-heading';
        heading.textContent = item.heading;
        menu.appendChild(heading);
        return;
      }
      menu.appendChild(entry(item));
    });

    attach(menu, at);
    return menu;
  }

  /**
   * One menu entry. Built with createElement throughout: labels are often
   * user input — a device name, a group name — and this way they cannot be
   * markup at all rather than being escaped on the way in.
   */
  function entry(item) {
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    if (item.danger) button.classList.add('ctx-danger');
    if (item.icon) {
      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = item.icon;
      button.appendChild(icon);
    }
    button.appendChild(document.createTextNode(item.label || ''));
    if (item.value) {
      const shown = document.createElement('span');
      shown.className = 'ctx-value';
      shown.textContent = item.value;
      button.appendChild(shown);
    }
    if (item.title) button.title = item.title;
    if (item.disabled) {
      // Present but grey (#262): an entry that vanishes leaves someone
      // wondering where it went; one that explains itself does not.
      button.disabled = true;
    } else {
      button.addEventListener('click', (e) => {
        e.stopPropagation();
        close();
        if (typeof item.onClick === 'function') item.onClick(e);
      });
    }
    return button;
  }

  /**
   * Adopt a menu built elsewhere: put it on the page at `at`, clamped to the
   * viewport, and give it the shared dismissal and keyboard handling.
   *
   * Replaces any menu already open. The element need not be in the DOM yet.
   */
  function attach(menu, at) {
    close();

    if (!menu.getAttribute('role')) menu.setAttribute('role', 'menu');
    menu.tabIndex = -1;
    menu.querySelectorAll('button').forEach(b => {
      if (!b.getAttribute('role')) b.setAttribute('role', 'menuitem');
    });

    if (!menu.isConnected) document.body.appendChild(menu);
    place(menu, at);

    restoreFocus = document.activeElement;
    current = menu;

    // Deferred a tick so the click (or contextmenu) that opened the menu
    // does not also close it as it finishes bubbling.
    setTimeout(() => {
      if (current !== menu) return;
      document.addEventListener('click', onDocumentClick, true);
      document.addEventListener('contextmenu', onDocumentContext, true);
      document.addEventListener('keydown', onKeydown, true);
      window.addEventListener('resize', close);
      // Focus the menu itself rather than its first entry: the arrows then
      // work, but nothing is highlighted before the person has chosen to
      // steer with the keyboard.
      try { menu.focus({ preventScroll: true }); } catch (_) { /* detached */ }
    }, 0);
    return menu;
  }

  /** Clamp to the viewport on both axes, with an 8px margin. */
  function place(menu, at) {
    let x = 8, y = 8;
    if (at && typeof at.getBoundingClientRect === 'function' && !('clientX' in at)) {
      const rect = at.getBoundingClientRect();
      x = rect.left;
      y = rect.bottom + 4;
    } else if (at) {
      x = at.clientX || 0;
      y = at.clientY || 0;
    }
    const width  = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth  - width  - 8))}px`;
    menu.style.top  = `${Math.max(8, Math.min(y, window.innerHeight - height - 8))}px`;
  }

  /**
   * Close the open menu, and any stragglers.
   *
   * By class as well as by reference: a submenu shares the class so that it
   * is cleaned up with its parent, and the sweep is also what makes this
   * safe to call from code that never went through `open`.
   */
  function close() {
    document.removeEventListener('click', onDocumentClick, true);
    document.removeEventListener('contextmenu', onDocumentContext, true);
    document.removeEventListener('keydown', onKeydown, true);
    window.removeEventListener('resize', close);

    const hadFocusInside = current && current.contains(document.activeElement);
    document.querySelectorAll('.tab-context-menu').forEach(el => el.remove());
    current = null;

    if (hadFocusInside && restoreFocus && restoreFocus.isConnected) {
      try { restoreFocus.focus({ preventScroll: true }); } catch (_) { /* gone */ }
    }
    restoreFocus = null;
  }

  function isOpen() { return !!current; }

  function onDocumentClick(e) {
    if (current && current.contains(e.target)) return;
    // A click inside a submenu that shares the class is inside "the menu".
    if (e.target.closest && e.target.closest('.tab-context-menu')) return;
    close();
  }

  function onDocumentContext(e) {
    if (current && current.contains(e.target)) { e.preventDefault(); return; }
    // Right-clicking elsewhere lets whatever is there open its own menu;
    // ours simply goes.
    close();
  }

  function onKeydown(e) {
    if (!current) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === 'Tab') { close(); return; }

    const inMenu = current.contains(document.activeElement);
    const items = [...current.querySelectorAll('button:not(:disabled)')];
    if (!items.length) return;
    const index = items.indexOf(document.activeElement);

    let next = null;
    if (e.key === 'ArrowDown') next = index < 0 ? 0 : (index + 1) % items.length;
    else if (e.key === 'ArrowUp') next = index < 0 ? items.length - 1 : (index - 1 + items.length) % items.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = items.length - 1;
    else if ((e.key === 'Enter' || e.key === ' ') && inMenu && index >= 0) {
      e.preventDefault();
      items[index].click();
      return;
    } else {
      return;
    }
    e.preventDefault();
    items[next].focus();
    // A long menu scrolls (#437); the focused entry must stay in view.
    try { items[next].scrollIntoView({ block: 'nearest' }); } catch (_) { /* old engine */ }
  }

  window.shellmateMenu = { open, attach, close, isOpen };
})();
