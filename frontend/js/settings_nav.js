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
    ['ai providers',        'smart_toy'],
    ['knowledge base',      'description'],
    ['alerts',              'warning'],
    ['configuration capture', 'save'],
    ['interface',           'settings'],
    ['behavior',            'settings'],
    ['behaviour',           'settings'],
    ['logging',             'description'],
  ];


  /**
   * Stockton entries that are not registry settings.
   *
   * The registry is the source for everything else, so this list is short by
   * design — anything on it is something that could not be expressed as a
   * scalar with a default and a range.
   */
  const EXTRAS = [
    {
      key:   'ai.prompts',
      label: 'The system prompts',
      terms: ['prompt', 'prompts', 'system prompt', 'persona', 'tshoot', 'learn'],
    },
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
        const top = [...body.querySelectorAll('.settings-section')]
          .filter(el => !el.parentElement.closest('.settings-section'));
        if (top.length !== sections.length) build();
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

    // Top-level sections only. A section nested inside another is a block
    // belonging to its parent, not a destination of its own — and treating it
    // as one hides it, because show() hides every section that is not the
    // active one. That is precisely how the prompt editor came to be present,
    // correctly built and invisible once before; it now sits inside the AI
    // section, so this is the rule that keeps it visible.
    sections = [...body.querySelectorAll('.settings-section')]
      .filter(el => !el.parentElement.closest('.settings-section'));
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
    const stockton = document.getElementById('settings-stockton-results');
    if (stockton) stockton.remove();
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
        // Tooltip text counts as part of the row. It is where the consequence
        // of a setting is written — searching for "clipboard" ought to find
        // Copy on Select, and only the tooltip says the word.
        const tips = [...row.querySelectorAll('[data-tip]')]
          .map(el => el.getAttribute('data-tip')).join(' ');
        const text = ((row.textContent || '') + ' ' + tips).toLowerCase();
        const matches = titleMatches || text.includes(query);
        row.classList.toggle('settings-hidden-by-search', !matches);
        if (matches) anyVisible = true;
      });

      section.classList.toggle('hidden', !anyVisible && !titleMatches);
    });

    showExtras(query);
  }

  /**
   * The advanced settings need no special case in search any more (#135).
   *
   * They lived in their own panel, so a search here fetched /api/advanced and
   * offered badged "Also in Stockton" results pointing at the other place.
   * They are ordinary `.settings-section` elements with ordinary
   * `.setting-row` children now, so the filter above finds them exactly as it
   * finds everything else — and a second search path that could disagree with
   * the first is the drift worth removing.
   *
   * EXTRAS survives for the prompt editor alone: it is prose in prompts.json
   * rather than a registry entry, so nothing generates a row for it, and
   * somebody searching "prompt" must not simply find nothing.
   */
  function showExtras(query) {
    const body = panel.querySelector('.panel-body');
    if (!body) return;

    const existing = document.getElementById('settings-extra-results');
    if (existing) existing.remove();
    if (query.length < 2) return;

    const matches = EXTRAS.filter(extra =>
      extra.terms.some(term => term.includes(query) || query.includes(term)));
    if (!matches.length) return;

    // Deliberately NOT class "settings-section": the observer rebuilds the
    // whole nav whenever that count changes, and rebuilding calls show(),
    // which removes this block again. It ate its own results.
    const block = document.createElement('section');
    block.className = 'settings-extra-results';
    block.id = 'settings-extra-results';

    const heading = document.createElement('h3');
    heading.className = 'settings-section-title';
    heading.textContent = 'Elsewhere in Settings';
    block.appendChild(heading);

    matches.forEach(extra => {
      const row = document.createElement('div');
      row.className = 'setting-row';

      const label = document.createElement('span');
      label.className = 'setting-label';
      label.textContent = extra.label;

      const go = document.createElement('button');
      go.type = 'button';
      go.className = 'btn-tertiary';
      go.textContent = 'Show me';
      go.addEventListener('click', () => {
        const target = document.getElementById('prompt-editor-block');
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });

      row.append(label, go);
      block.appendChild(row);
    });

    body.appendChild(block);
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
