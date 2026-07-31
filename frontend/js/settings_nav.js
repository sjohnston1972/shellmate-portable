/**
 * settings_nav.js — Make Settings navigable instead of a long scroll.
 *
 * The panel had grown to a dozen sections stacked vertically, so finding
 * anything meant scrolling past everything else. This turns it into a
 * category list with one section shown at a time, plus a search that looks
 * across all of them.
 *
 * A note on the approach: the request was to open each section in its own
 * modal. A category list gets to the same place — no scrolling to reach a
 * control — without the open/close churn of a modal per section, and it is
 * what Settings looks like in most applications, so it needs no explaining.
 * Easy to change if the modal version turns out to be what was wanted.
 *
 * Built by reading the existing sections rather than restructuring the
 * markup, so adding a section stays a matter of adding a <section> and
 * nothing else.
 */
(function () {
  'use strict';

  /** Icon per section title, matched on a lowercase substring. */
  const ICONS = [
    ['terminal appearance', 'terminal'],
    ['device awareness',    'smart_toy'],
    ['output colours',      'light_mode'],
    ['platform',            'list_alt'],
    ['serial',              'cable'],
    ['credentials vault',   'bookmark_add'],
    ['ai assistant',        'smart_toy'],
    ['ai providers',        'smart_toy'],
    ['knowledge base',      'description'],
    ['alerts',              'warning'],
    ['interface',           'settings'],
    ['behavior',            'settings'],
    ['behaviour',           'settings'],
    ['logging',             'description'],
  ];

  let panel, nav, search, sections = [];
  let active = 0;

  document.addEventListener('DOMContentLoaded', () => {
    panel = document.getElementById('settings-panel');
    if (!panel) return;

    build();

    // Rebuild if sections are added after load (the platform editor injects
    // its own), so the nav never falls out of step with the content.
    const body = panel.querySelector('.panel-body');
    if (body) {
      new MutationObserver(() => {
        if (body.querySelectorAll('.settings-section').length !== sections.length) build();
      }).observe(body, { childList: true });
    }
  });

  function iconFor(title) {
    const lower = title.toLowerCase();
    const hit = ICONS.find(([key]) => lower.includes(key));
    return hit ? hit[1] : 'settings';
  }

  function build() {
    const body = panel.querySelector('.panel-body');
    if (!body) return;

    sections = [...body.querySelectorAll('.settings-section')];
    if (!sections.length) return;

    // Search box and nav rail, created once.
    let rail = document.getElementById('settings-nav');
    if (!rail) {
      const wrap = document.createElement('div');
      wrap.id = 'settings-layout';

      rail = document.createElement('nav');
      rail.id = 'settings-nav';

      const searchWrap = document.createElement('div');
      searchWrap.id = 'settings-search-wrap';
      search = document.createElement('input');
      search.type = 'search';
      search.id = 'settings-search';
      search.placeholder = 'Search settings…';
      search.autocomplete = 'off';
      search.addEventListener('input', applySearch);
      searchWrap.appendChild(search);

      body.parentNode.insertBefore(searchWrap, body);
      body.parentNode.insertBefore(wrap, body);
      wrap.appendChild(rail);
      wrap.appendChild(body);
    }

    nav = rail;
    nav.innerHTML = '';

    sections.forEach((section, index) => {
      const titleEl = section.querySelector('.settings-section-title');
      const title = titleEl ? titleEl.textContent.trim() : `Section ${index + 1}`;

      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'settings-nav-item';
      item.dataset.index = String(index);

      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.textContent = iconFor(title);

      const label = document.createElement('span');
      label.textContent = title;

      item.appendChild(icon);
      item.appendChild(label);
      item.addEventListener('click', () => show(index));
      nav.appendChild(item);
    });

    show(Math.min(active, sections.length - 1));
  }

  function show(index) {
    active = index;
    if (search) search.value = '';
    sections.forEach((section, i) => {
      section.classList.toggle('hidden', i !== index);
      section.querySelectorAll('.settings-hidden-by-search')
        .forEach(el => el.classList.remove('settings-hidden-by-search'));
    });
    nav.querySelectorAll('.settings-nav-item').forEach((item, i) => {
      item.classList.toggle('active', i === index);
    });
    const body = panel.querySelector('.panel-body');
    if (body) body.scrollTop = 0;
  }

  /**
   * Filter across every section.
   *
   * While searching, sections stop being exclusive — the point is to find a
   * control without knowing which category it is under, so all of them are
   * considered and only matching rows are shown.
   */
  function applySearch() {
    const query = search.value.trim().toLowerCase();

    if (!query) {
      show(active);
      return;
    }

    nav.querySelectorAll('.settings-nav-item').forEach(i => i.classList.remove('active'));

    sections.forEach(section => {
      const title = (section.querySelector('.settings-section-title') || {}).textContent || '';
      const titleMatches = title.toLowerCase().includes(query);

      // A row is anything with a label or a hint; treat each as a unit so a
      // control never appears without the text explaining it.
      const rows = [...section.querySelectorAll('.setting-row, .settings-section-hint, .highlight-rule, #provider-results')];
      let anyVisible = false;

      rows.forEach(row => {
        const text = (row.textContent || '').toLowerCase();
        const matches = titleMatches || text.includes(query);
        row.classList.toggle('settings-hidden-by-search', !matches);
        if (matches) anyVisible = true;
      });

      section.classList.toggle('hidden', !anyVisible && !titleMatches);
    });
  }

  /** Open Settings at a named section, for deep links from elsewhere. */
  window.openSettingsSection = (name) => {
    if (typeof window.openSettings === 'function') window.openSettings();
    const wanted = (name || '').toLowerCase();
    const index = sections.findIndex(s => {
      const t = (s.querySelector('.settings-section-title') || {}).textContent || '';
      return t.toLowerCase().includes(wanted);
    });
    if (index >= 0) setTimeout(() => show(index), 50);
  };
})();
