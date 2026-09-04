/**
 * ansible_view.js — The Ansible view: which area is showing, and nothing else (#586).
 *
 * Ansible outgrew a side panel. A dashboard, a playbook library, a template
 * being filled in, a run streaming task by task — that is somewhere you go
 * and work for a while, not a drawer you open over a terminal. So it is a
 * third layer in the pane, a peer of the dashboard and the terminals: the
 * sessions carry on behind it, and leaving puts you back exactly where you
 * were.
 *
 * This file is deliberately small. It owns:
 *
 * - opening and closing the stage, and remembering what was underneath;
 * - which area is visible, and the address bar's memory of that;
 * - one shared cache of the runner's state and the library, fetched once
 *   and handed to every area rather than fetched eight times;
 * - the seam: `window.ansibleView.area(name, handlers)`.
 *
 * It owns none of the areas' contents. Each area is a `<section
 * data-av-area>` in the markup with its own script that registers itself
 * here, so the areas could be built separately without eight scripts
 * fighting over one panel. An area that fails to load leaves its section
 * saying so, and the other seven still work — which is the point of the
 * seam and not an accident of it.
 */

(function () {
  'use strict';

  /** Registered areas, by name: { onShow, onData, title }. */
  const areas = new Map();

  /** The shared cache. One fetch, eight readers. */
  const state = {
    overview: null,     // runner + library counts + recent jobs
    library: null,      // templates, environments, repositories
    keys: null,         // names only; there is no endpoint that returns a value
    loading: false,
    error: '',
  };

  let current = '';
  let open = false;

  // -- Small helpers every area needs, so eight files do not each write them.

  /**
   * A JSON call that turns a failure into something worth reading.
   *
   * FastAPI puts the reason in `detail`, and pydantic puts it in a list of
   * objects. Both arrive here as one sentence, because "500" on a screen
   * has never told anybody anything.
   */
  async function json(url, options) {
    const response = await fetch(url, options);
    let body = null;
    try {
      body = await response.json();
    } catch (e) {
      body = null;
    }
    if (!response.ok) {
      let detail = (body && body.detail) || '';
      if (Array.isArray(detail)) {
        detail = detail.map(d => (d && d.msg) || String(d)).join('; ');
      }
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  /** POST some JSON, which is what most of these areas do. */
  function post(url, payload) {
    return json(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
  }

  function del(url) {
    return json(url, { method: 'DELETE' });
  }

  /** Replace an element's children with nothing. */
  function clear(element) {
    while (element && element.firstChild) element.removeChild(element.firstChild);
    return element;
  }

  /**
   * Build an element without a string of HTML.
   *
   * Everything in these areas is a name somebody typed — a template, a host,
   * a failure message from a device. innerHTML with any of that in it is an
   * injection waiting for the one playbook name with a bracket in it.
   */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;   // callers pass no user text
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, value === true ? '' : String(value));
    });
    (Array.isArray(children) ? children : children ? [children] : [])
      .forEach(child => {
        if (child === null || child === undefined || child === false) return;
        node.appendChild(typeof child === 'string'
          ? document.createTextNode(child) : child);
      });
    return node;
  }

  /** A Material Symbols glyph. Names must be in the committed subset. */
  function icon(name) {
    return el('span', { class: 'material-symbols-outlined', text: name });
  }

  /** What an area shows instead of a blank rectangle. */
  function empty(message, action) {
    return el('div', { class: 'av-empty' }, [message, action || null]);
  }

  /**
   * Say something went wrong, using ShellMate's own dialog.
   *
   * There is no toast system to lean on here, and a silent failure in a
   * view full of forms is worse than an interruption: somebody would sit
   * looking at a template that did not save.
   */
  function toast(message, kind) {
    if (kind === 'error' && window.shellmateDialog) {
      window.shellmateDialog.alert({ title: 'Ansible', body: String(message) });
    } else {
      console.log('[ansible]', message);
    }
  }

  // -- The runner's state, said once at the top ----------------------------

  /**
   * Paint the header pill.
   *
   * Three states, not two. "Not set up", "cannot be reached" and "reachable
   * but refusing us" have three different fixes, and collapsing them into
   * "disconnected" sends somebody to a firewall when the answer is a token
   * under Settings.
   */
  function paintRunner() {
    const pill = document.getElementById('av-runner-pill');
    const detail = document.getElementById('av-runner-detail');
    if (!pill || !detail) return;
    const runner = (state.overview || {}).runner || {};

    let cls = 'av-pill-unknown';
    let label = 'Checking…';
    let says = '';

    if (state.loading && !state.overview) {
      // leave it
    } else if (!runner.configured) {
      cls = 'av-pill-unknown';
      label = 'Not set up';
      says = runner.detail || 'No runner address yet. Settings → Ansible.';
    } else if (runner.reachable && runner.authenticated !== false) {
      // Connected, but say on what terms. A run going over a connection
      // nothing is checking is a thing the person who chose that should be
      // reminded of, not spared.
      const unverified = runner.encrypted && runner.verified === false;
      cls = unverified ? 'av-pill-warn' : 'av-pill-ok';
      label = unverified ? 'Connected, unverified' : 'Connected';
      says = runner.url || '';
      if (typeof runner.playbooks === 'number') {
        says += `${says ? ' — ' : ''}${runner.playbooks} playbook`
             + `${runner.playbooks === 1 ? '' : 's'}`;
      }
      if (unverified) {
        says += ' — the certificate is not being checked.';
      } else if (runner.encrypted === false) {
        says += ' — plain HTTP, so the token crosses in the clear.';
      }
    } else if (runner.reachable) {
      cls = 'av-pill-warn';
      label = 'Refusing us';
      says = runner.detail || 'The runner is there but will not accept ShellMate.';
    } else if (runner.kind === 'certificate') {
      // Not a reachability problem. Something answered; ShellMate would not
      // trust it. Filing that under "unreachable" sends somebody to the
      // firewall for a problem in a file on their own disk.
      cls = 'av-pill-bad';
      label = 'Certificate refused';
      says = runner.detail || "The runner's certificate was not accepted.";
    } else {
      cls = 'av-pill-bad';
      label = 'Unreachable';
      says = runner.detail || `Could not reach ${runner.url || 'the runner'}.`;
    }

    pill.className = `av-pill ${cls}`;
    pill.textContent = label;
    detail.textContent = state.error || says;
    detail.title = detail.textContent;
  }

  // -- Loading -------------------------------------------------------------

  /**
   * Fetch what every area shares, once.
   *
   * The library half is fetched even when the runner is unreachable: a
   * template you wrote is still worth editing when the container is off,
   * and an area that blanks itself because something *else* failed is the
   * kind of coupling that makes a tool feel fragile.
   */
  async function load(force) {
    if (state.loading) return;
    state.loading = true;
    state.error = '';
    paintRunner();
    try {
      const [overview, library, keys] = await Promise.all([
        json('/api/ansible/overview').catch(e => ({ _error: String(e.message || e) })),
        json('/api/ansible/catalogue').catch(() => ({ templates: [], environments: [], repositories: [] })),
        json('/api/ansible/keys').catch(() => ({ keys: [] })),
      ]);
      if (overview && overview._error) state.error = overview._error;
      else state.overview = overview;
      state.library = library;
      state.keys = keys;
    } finally {
      state.loading = false;
    }
    paintRunner();
    areas.forEach(handlers => {
      if (typeof handlers.onData === 'function') {
        try {
          handlers.onData(state);
        } catch (e) {
          console.error('[ansible] area failed on new data', e);
        }
      }
    });
    return state;
  }

  // -- Which area is showing ----------------------------------------------

  function show(name) {
    const sections = Array.from(document.querySelectorAll('#av-body .av-area'));
    if (!sections.length) return;
    const known = sections.map(s => s.dataset.avArea);
    const target = known.includes(name) ? name : known[0];

    sections.forEach(section => {
      section.hidden = section.dataset.avArea !== target;
    });
    document.querySelectorAll('#av-nav .av-tab').forEach(tab => {
      if (tab.dataset.avGo === target) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    });

    const changed = current !== target;
    current = target;
    try {
      window.localStorage.setItem('shellmate.ansible.area', target);
    } catch (e) { /* private mode; the default area is fine */ }

    const handlers = areas.get(target);
    if (handlers && typeof handlers.onShow === 'function') {
      try {
        handlers.onShow(state, changed);
      } catch (e) {
        console.error('[ansible] area failed to show', e);
      }
    }
    document.dispatchEvent(new CustomEvent('shellmate:ansible-area', {
      detail: { area: target },
    }));
  }

  /**
   * Register an area.
   *
   * `onShow(state, changed)` runs when the area becomes visible; `onData`
   * runs whenever the shared cache is refreshed, whether or not the area
   * is on screen. Registering after the view has already opened still
   * works — the area is told immediately — so load order does not matter.
   */
  function area(name, handlers) {
    areas.set(name, handlers || {});
    if (open && current === name && typeof handlers.onShow === 'function') {
      handlers.onShow(state, true);
    } else if (state.overview && typeof handlers.onData === 'function') {
      handlers.onData(state);
    }
  }

  // -- Opening and closing -------------------------------------------------

  /**
   * Open the view over whatever is in the pane.
   *
   * The terminals are not touched. They are behind this layer, still
   * connected and still receiving — a device mid-reload has to be there
   * when you come back, and hiding a terminal by display would collapse
   * xterm's measured geometry and need a refit on return.
   */
  function openView(areaName) {
    const stage = document.getElementById('ansible-stage');
    if (!stage) return;
    open = true;
    stage.hidden = false;
    document.body.classList.add('ansible-view-open');

    let start = areaName;
    if (!start) {
      try {
        start = window.localStorage.getItem('shellmate.ansible.area') || 'dashboard';
      } catch (e) {
        start = 'dashboard';
      }
    }
    show(start);
    load();
    document.dispatchEvent(new CustomEvent('shellmate:ansible-open'));
  }

  function closeView() {
    const stage = document.getElementById('ansible-stage');
    if (!stage) return;
    open = false;
    stage.hidden = true;
    document.body.classList.remove('ansible-view-open');
    document.dispatchEvent(new CustomEvent('shellmate:ansible-close'));
  }

  function toggle() {
    if (open) closeView(); else openView();
  }

  // -- Wiring --------------------------------------------------------------

  function wire() {
    const stage = document.getElementById('ansible-stage');
    if (!stage) return;

    document.querySelectorAll('#av-nav .av-tab').forEach(tab => {
      tab.addEventListener('click', () => show(tab.dataset.avGo));
    });

    const refresh = document.getElementById('av-refresh');
    if (refresh) {
      refresh.addEventListener('click', async () => {
        refresh.disabled = true;
        try {
          // Areas that keep their own state — Playbooks holds a live run —
          // reload themselves rather than being re-rendered from the shared
          // cache, which does not know about a job in flight.
          document.dispatchEvent(new CustomEvent('shellmate:ansible-refresh'));
          await load(true);
          const handlers = areas.get(current);
          if (handlers && typeof handlers.onShow === 'function') {
            handlers.onShow(state, false);
          }
        } finally {
          refresh.disabled = false;
        }
      });
    }

    const settings = document.getElementById('av-settings');
    if (settings) {
      settings.addEventListener('click', () => {
        // The runner's address and token stay in Settings with every other
        // credential, rather than being duplicated here. Two places to set
        // one token is two places for it to be wrong.
        if (window.openSettings) window.openSettings('ansible');
        else toast('Settings → Ansible', 'info');
      });
    }

    const close = document.getElementById('av-close');
    if (close) close.addEventListener('click', closeView);

    // Escape leaves the view, unless something modal is on top of it.
    //
    // Matched on aria-modal as well as on the overlay classes, because not
    // every dialog in ShellMate wears one: the run dialog is a bare div
    // with role="dialog", and without this a single Escape closed both it
    // and the view underneath — losing the screen somebody was working on
    // to a keystroke meant for a form.
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !open) return;
      const modal = document.querySelector(
        '.modal-overlay:not(.hidden), .panel-overlay:not(.hidden), '
        + '[aria-modal="true"]:not(.hidden), [role="dialog"]:not(.hidden)');
      if (modal && modal.offsetParent !== null) return;
      closeView();
    });

    // The sidebar link opens the view rather than the old drawer.
    const link = document.getElementById('sidebar-link-ansible');
    if (link) {
      const fresh = link.cloneNode(true);        // drop ansible.js's handler
      link.parentNode.replaceChild(fresh, link);
      fresh.addEventListener('click', (event) => {
        event.preventDefault();
        toggle();
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }

  window.ansibleView = {
    open: openView, close: closeView, toggle, show, area, load, state,
    json, post, del, el, icon, clear, empty, toast, paintRunner,
    get current() { return current; },
    get isOpen() { return open; },
  };
})();
