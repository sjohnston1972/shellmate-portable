/**
 * layout.js — Tiling. Show several sessions at once instead of one at a time.
 *
 * The tab strip stays exactly as it was; this adds the ability to put more
 * than one of those tabs on screen together. Watching a routing adjacency come
 * back up on one device while clearing it on another is the whole point, and
 * alt-tabbing between them is precisely the wrong tool for it.
 *
 * How it works
 * ------------
 * Every terminal already lives in its own container inside #terminals-container,
 * with only the active one displayed. Tiling makes that container a CSS grid
 * and displays several. Nothing is reparented: an xterm.js instance moved to a
 * different parent has to be re-measured and re-fitted, and the renderer does
 * not always survive it intact. Instead each visible container is given a
 * `grid-area`, which is a style change and nothing more — so a session being
 * tiled, untiled or moved between panes never loses a byte of scrollback.
 *
 * A *pane* is a slot in the layout. A *tab* is a session. The assignment
 * between them is held here, and is deliberately not derived from tab order:
 * having arranged four devices across a quad view, adding a fifth tab must not
 * rearrange them.
 *
 * One pane is focused. That is what the status bar describes, what the AI
 * panel reads, and what the keyboard talks to — so "the active tab" continues
 * to mean one specific thing however many are visible.
 */
(function () {
  'use strict';

  /**
   * The available layouts.
   *
   * `areas` is written the way it reads on screen: one string per grid row,
   * each letter a pane. Asymmetric layouts fall straight out of repeating a
   * letter across cells, which is why they cost nothing extra here.
   */
  const LAYOUTS = [
    { id: 'single', name: 'Single',        panes: 1,
      cols: '1fr',         rows: '1fr',     areas: ['a'] },
    { id: 'cols2',  name: 'Side by side',  panes: 2,
      cols: '1fr 1fr',     rows: '1fr',     areas: ['a b'] },
    { id: 'rows2',  name: 'Stacked',       panes: 2,
      cols: '1fr',         rows: '1fr 1fr', areas: ['a', 'b'] },
    { id: 'cols3',  name: 'Three columns', panes: 3,
      cols: '1fr 1fr 1fr', rows: '1fr',     areas: ['a b c'] },
    { id: 'main2',  name: 'Main and two',  panes: 3,
      cols: '2fr 1fr',     rows: '1fr 1fr', areas: ['a b', 'a c'] },
    { id: 'twoRow', name: 'Two over one',  panes: 3,
      cols: '1fr 1fr',     rows: '1fr 1fr', areas: ['a b', 'c c'] },
    // The mirror of the one above, and the commoner arrangement of the two:
    // the device being worked on across the top, the ones it depends on
    // beneath it. Placed beside its twin rather than appended, which shifts
    // Quad's shortcut from Ctrl+Alt+7 to Ctrl+Alt+8 — worth it, since the
    // menu lists them in this order and a mirrored pair split across the
    // list reads as two unrelated options.
    { id: 'oneRow', name: 'One over two',  panes: 3,
      cols: '1fr 1fr',     rows: '1fr 1fr', areas: ['a a', 'b c'] },
    { id: 'quad',   name: 'Quad',          panes: 4,
      cols: '1fr 1fr',     rows: '1fr 1fr', areas: ['a b', 'c d'] },
  ];

  const PANE_LETTERS = ['a', 'b', 'c', 'd'];

  let currentId = 'single';
  /** @type {Array<string|null>} pane index → sessionId */
  let assignment = [null];
  let focusedPane = 0;

  let container, button, menu;

  document.addEventListener('DOMContentLoaded', () => {
    container = document.getElementById('terminals-container');
    button    = document.getElementById('btn-layout');
    if (!container || !button) return;

    button.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(); });

    // The chosen layout is restored, but only the choice — which session sits
    // in which pane is meaningless across a reload, since none of them survive
    // it. Kept in settings.json rather than localStorage so it travels with
    // the data folder; prefs.js calls set() again once those have loaded.
    const saved = window.shellmatePrefs
      ? window.shellmatePrefs.get('default_layout', 'single') : 'single';
    if (saved && byId(saved)) currentId = saved;
    assignment = new Array(byId(currentId).panes).fill(null);

    document.addEventListener('keydown', onKeydown);

    // The split divider and the AI panel both resize the terminal area without
    // the window changing size, so a window resize listener alone is not
    // enough. Observing the container catches every cause at once.
    if (window.ResizeObserver) {
      let pending = false;
      new ResizeObserver(() => {
        if (pending) return;
        pending = true;
        requestAnimationFrame(() => { pending = false; refit(); });
      }).observe(container);
    }

    render();
  });

  function byId(id) { return LAYOUTS.find(l => l.id === id); }
  function layout() { return byId(currentId); }

  // -------------------------------------------------------------------------
  // Assignment
  // -------------------------------------------------------------------------

  /** Session ids currently on screen, in pane order, with the gaps removed. */
  function visible() { return assignment.filter(Boolean); }

  function paneOf(sessionId) { return assignment.indexOf(sessionId); }

  /**
   * Put every open tab that is not already on screen into an empty pane.
   *
   * Called after a layout change or a tab closing. Tabs already placed keep
   * their pane — the arrangement is the user's, and quietly reshuffling it
   * because a session ended would be its own small betrayal.
   */
  function fillEmptyPanes() {
    const open = (window.getOpenSessionIds ? window.getOpenSessionIds() : []);
    const placed = new Set(visible());
    const spare = open.filter(id => !placed.has(id));

    for (let i = 0; i < assignment.length && spare.length; i++) {
      if (!assignment[i]) assignment[i] = spare.shift();
    }
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  function render() {
    const l = layout();

    // With nothing open there is nothing to tile, and the welcome screen is
    // already offering a connection — better than a grid of empty boxes drawn
    // over the top of it. The layout is still *chosen*: it is remembered in
    // settings.json and takes effect the moment a tab opens.
    const anySessions = (window.getOpenSessionIds ? window.getOpenSessionIds() : []).length > 0;
    const tiled = l.panes > 1 && anySessions;

    container.classList.toggle('tiled', tiled);
    if (tiled) {
      container.style.gridTemplateColumns = l.cols;
      container.style.gridTemplateRows    = l.rows;
      container.style.gridTemplateAreas   = l.areas.map(r => `"${r}"`).join(' ');
    } else {
      container.style.gridTemplateColumns = '';
      container.style.gridTemplateRows    = '';
      container.style.gridTemplateAreas   = '';
    }

    // Placeholders are rebuilt each time rather than kept in step, because
    // there are at most three of them and correctness is worth more here.
    container.querySelectorAll('.pane-empty').forEach(el => el.remove());

    const shown = new Set(visible());
    container.querySelectorAll('.terminal-container').forEach(el => {
      const sessionId = el.id.replace('terminal-', '');
      const pane = paneOf(sessionId);
      el.classList.toggle('active', shown.has(sessionId));
      el.classList.toggle('pane-focused', tiled && pane === focusedPane);
      el.style.gridArea = (tiled && pane >= 0) ? PANE_LETTERS[pane] : '';
    });

    if (tiled) {
      assignment.forEach((sessionId, i) => {
        if (!sessionId) container.appendChild(placeholder(i));
      });
    }

    updateButton();

    // The strip marks the one tab with the keyboard. Under a tiled layout
    // several others are also on screen, and without saying which, the strip
    // implies the rest are hidden when they are not.
    window.dispatchEvent(new CustomEvent('shellmate:layout-rendered', {
      detail: {
        visible: tiled ? visible() : [],
        focused: assignment[focusedPane] || null,
      },
    }));

    refit();
  }

  /**
   * An empty pane.
   *
   * Only ever seen when the layout has more panes than there are sessions —
   * every other case is filled automatically. It offers nothing clickable
   * (#229): the "New connection" button it used to carry could never fire —
   * the pane's own mousedown re-rendered the placeholders, so the pressed
   * button was detached before the click event existed. The hint names the
   * shortcut instead, which is the working route.
   */
  function placeholder(paneIndex) {
    const el = document.createElement('div');
    el.className = 'pane-empty' + (paneIndex === focusedPane ? ' pane-focused' : '');
    el.style.gridArea = PANE_LETTERS[paneIndex];
    el.dataset.pane = String(paneIndex);

    const hint = document.createElement('div');
    hint.className = 'pane-empty-hint';
    hint.textContent = 'Empty pane — Ctrl+T for a new connection';
    el.appendChild(hint);

    el.addEventListener('mousedown', () => {
      // Only when the focus actually moves. Re-rendering on every press is
      // what used to tear the placeholder out from under its own button.
      if (focusedPane !== paneIndex) { focusedPane = paneIndex; render(); }
    });
    return el;
  }

  /**
   * Put a session in a pane, and give the pane it came from whatever was there.
   *
   * A swap rather than a move-and-leave-a-hole: asking for two devices to
   * change places is the reason anyone reaches for this, and emptying a pane
   * that has a perfectly good session for it would just be work to undo.
   */
  function place(paneIndex, sessionId) {
    const old = paneOf(sessionId);
    const displaced = assignment[paneIndex];

    assignment[paneIndex] = sessionId;
    if (old >= 0 && old !== paneIndex) assignment[old] = displaced || null;

    focusedPane = paneIndex;
    render();
    if (typeof window.switchToTabBySessionId === 'function') {
      window.switchToTabBySessionId(sessionId);
    }
  }

  /** Refit every visible terminal and tell the far end its new size. */
  function refit() {
    if (typeof window.refitTerminals === 'function') window.refitTerminals(visible());
  }

  // -------------------------------------------------------------------------
  // The picker
  // -------------------------------------------------------------------------

  function updateButton() {
    button.title = `Layout: ${layout().name} (Ctrl+Alt+1…${LAYOUTS.length})`;
    button.innerHTML = '';
    button.appendChild(preview(layout(), true));
  }

  /**
   * A miniature of the layout, built from the same grid definition.
   *
   * Drawn rather than iconised because the whole question the user is asking —
   * what will the screen look like — is answered by a picture of it, and
   * because it keeps the asymmetric options honest: the preview cannot
   * disagree with the layout when both come from one definition, and adding
   * one is an entry in LAYOUTS and nothing else.
   */
  function preview(l, small) {
    const el = document.createElement('span');
    el.className = 'layout-preview' + (small ? ' layout-preview-sm' : '');
    el.style.gridTemplateColumns = l.cols;
    el.style.gridTemplateRows    = l.rows;
    el.style.gridTemplateAreas   = l.areas.map(r => `"${r}"`).join(' ');
    for (let i = 0; i < l.panes; i++) {
      const cell = document.createElement('span');
      cell.style.gridArea = PANE_LETTERS[i];
      el.appendChild(cell);
    }
    return el;
  }

  function toggleMenu() {
    if (menu) { closeMenu(); return; }

    menu = document.createElement('div');
    menu.className = 'layout-menu';

    LAYOUTS.forEach((l, i) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'layout-menu-item' + (l.id === currentId ? ' active' : '');
      item.dataset.layout = l.id;

      const name = document.createElement('span');
      name.className = 'layout-menu-name';
      name.textContent = l.name;

      const shortcut = document.createElement('span');
      shortcut.className = 'layout-menu-key';
      shortcut.textContent = `Ctrl+Alt+${i + 1}`;

      item.append(preview(l), name, shortcut);
      item.addEventListener('click', () => { set(l.id); closeMenu(); });
      menu.appendChild(item);
    });

    document.body.appendChild(menu);
    const rect = button.getBoundingClientRect();
    menu.style.top  = `${rect.bottom + 4}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8))}px`;

    setTimeout(() => document.addEventListener('click', closeMenu, { once: true }), 0);
  }

  function closeMenu() {
    if (menu) { menu.remove(); menu = null; }
  }

  function onKeydown(e) {
    if (e.key === 'Escape' && menu) { closeMenu(); return; }
    // Ctrl+Alt+N picks a layout. Ctrl+N and Ctrl+Shift+N are both spoken for.
    if (e.ctrlKey && e.altKey && !e.shiftKey && e.key >= '1' && e.key <= '9') {
      const l = LAYOUTS[Number(e.key) - 1];
      if (l) { e.preventDefault(); set(l.id); }
    }
  }

  // -------------------------------------------------------------------------
  // Public surface
  // -------------------------------------------------------------------------

  /** Switch layout, keeping on screen whatever still fits. */
  function set(id) {
    const l = byId(id);
    if (!l) return;
    currentId = id;
    if (window.shellmatePrefs) window.shellmatePrefs.set('default_layout', id);

    // Shrinking drops the sessions that no longer have a pane. They are only
    // hidden — the session, its socket and its scrollback are untouched, and
    // the tab is still in the strip.
    const kept = visible().slice(0, l.panes);
    assignment = new Array(l.panes).fill(null);
    kept.forEach((sessionId, i) => { assignment[i] = sessionId; });

    focusedPane = Math.min(focusedPane, l.panes - 1);
    fillEmptyPanes();
    render();

    const focusedId = assignment[focusedPane];
    if (focusedId && typeof window.switchToTabBySessionId === 'function') {
      window.switchToTabBySessionId(focusedId);
    }
  }

  /**
   * Make a session visible and focused.
   *
   * Called whenever the active tab changes. A session already on screen keeps
   * its pane — switching to a tab you can see should move the focus, not
   * rearrange the screen underneath you.
   */
  function focus(sessionId) {
    if (!sessionId) return;
    const pane = paneOf(sessionId);
    if (pane >= 0) {
      focusedPane = pane;
    } else {
      // An empty pane is filled before an occupied one is displaced. Opening a
      // fourth connection into a quad view should land in the free quarter,
      // not evict whatever the user was last looking at.
      const empty = assignment.indexOf(null);
      focusedPane = empty >= 0 ? empty : focusedPane;
      assignment[focusedPane] = sessionId;
    }
    render();
  }

  /** A session has gone. Free its pane and pull in something that is waiting. */
  function forget(sessionId) {
    const pane = paneOf(sessionId);
    if (pane >= 0) assignment[pane] = null;
    fillEmptyPanes();
    render();
  }

  /** Everything is gone — used when the welcome screen is shown. */
  function clear() {
    assignment = assignment.map(() => null);
    render();
  }

  window.shellmateLayout = {
    set, focus, forget, clear, render, place,
    layouts:    () => LAYOUTS.map(l => ({ id: l.id, name: l.name, panes: l.panes })),
    current:    () => currentId,
    panes:      () => layout().panes,
    visible,
    focusedSessionId: () => assignment[focusedPane] || null,
  };
})();
