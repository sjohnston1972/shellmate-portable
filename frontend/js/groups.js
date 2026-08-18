/**
 * groups.js — Groups on the dashboard.
 *
 * A group is a tag with a face. Membership is the tag, so everything that
 * already understood tags — the tab strip's grouping, `/api/tags/{tag}/connect`,
 * search — understands groups without being told.
 *
 * This replaces the tag chip row. The two were the same feature seen twice:
 * chips filtered the grid by tag, and a group tile filters the grid by tag.
 * Keeping both would have meant two controls doing one job, differing only in
 * that one of them could be given a colour.
 *
 * **Diving in is a filter, not a navigation.** Nothing is torn down and no
 * session is touched — the grid narrows and a way back appears. That matters
 * because the dashboard is now reachable with twelve tabs running, and moving
 * around it must never cost a session.
 */
(function () {
  'use strict';

  /** The group currently opened, by key. '' means the whole dashboard. */
  let activeGroup = '';

  /** What the tree is filtered to (#183). '' shows everything. */
  let query = '';

  /**
   * Which branches a search forces open.
   *
   * A match deep in the tree is unreachable unless its ancestors are shown,
   * and that is the whole difference between filtering a tree and filtering a
   * list. Kept apart from `expanded` so a search never disturbs the shape
   * somebody arranged by hand — clearing the box puts it back as it was.
   */
  let matchOpen = new Set();

  /** Cached so a re-render does not wait on a request. */
  let groupCache = [];
  let colours = ['slate'];

  /**
   * The icons a group may carry (#180).
   *
   * Fetched rather than written here, because the list the picker offers and
   * the list the font is subsetted from have to be the same list — an icon
   * outside the subset renders as its own name in plain text and reports
   * nothing. `folder` alone until the fetch lands, which is the fallback the
   * backend uses too.
   */
  let icons = ['folder'];

  /** Whether the root node's children are shown (#260). Session-local — a
   *  folded tree on every launch would cost more than it saves. */
  let rootOpen = true;

  /** Whether group colours are switched on (#293). */
  function _coloursOn() {
    return ((window.shellmateSettings || {}).interface || {})
      .colourful_groups !== false;
  }

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('btn-new-group');
    // Routed by selection (#261): at root a new top-level group, inside a
    // group a child of it — matching the label renderTree paints on it.
    if (button) button.addEventListener('click', () => {
      const parent = activeGroup
        ? groupCache.find(g => g.key === activeGroup) : null;
      if (parent) newSubgroup(parent);
      else newGroup();
    });
    _bindPanelControls();

    // Escape lets go of a multi-selection. Only when one exists, and only
    // when nothing else on screen is what the key was aimed at (#352):
    // dialogs stop propagation themselves, but the overlay panels and the
    // context menus close on their own document-level Escape without
    // consuming it — and that press must not also throw away a selection
    // built with five careful clicks.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !selected.size) return;
      if (document.querySelector('.tab-context-menu')) return;
      const overlayOpen = [...document.querySelectorAll('[id$="-overlay"]')]
        .some(el => !el.classList.contains('hidden'));
      if (overlayOpen) return;
      _clearSelection();
      renderTree(profileCache);
    });
  });

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  // Recount the live badges when a session opens, closes or drops.
  //
  // One listener, because there were two and they ran in the wrong order:
  // this redraw was registered first and the `liveStale = true` below it
  // second, so the redraw used the state it was meant to invalidate and only
  // the *next* render was right. Both halves are here now, in the order they
  // have to happen (#203).
  window.addEventListener('shellmate:sessions-changed', () => {
    liveStale = true;
    // The tiles, not a re-fetch: which sessions are open cannot change which
    // connections are saved, and re-reading 5,000 profiles on every drop
    // would undo #188.
    if (typeof window.renderWelcomeTiles === 'function') {
      window.renderWelcomeTiles();
    }
  });

  function active() { return activeGroup; }

  /**
   * Whether the selection should paint device tiles (#177).
   *
   * Only a group with nothing below it. The tree is the navigation and the
   * tiles are the destination: painting five thousand of them at the top
   * level, or a whole site's worth for a parent, made the first paint the
   * slowest thing in the application and told nobody anything.
   */
  function showsTiles() {
    if (!activeGroup) return false;
    const node = _findNode(activeGroup);
    return Boolean(node) && node.children.length === 0;
  }

  /** The selected group's icon, for the hero (#179, #180). */
  function activeIcon() {
    const group = groupCache.find(x => x.key === activeGroup);
    return group ? (group.icon || 'folder') : '';
  }

  /** ['site-004', 'firewalls'] — the selection's position, for the hero (#179). */
  function activePath() {
    if (!activeGroup) return [];
    const group = groupCache.find(x => x.key === activeGroup);
    return (group ? group.name : activeGroup).split(SEPARATOR).filter(Boolean);
  }

  function activeName() {
    const group = groupCache.find(g => g.key === activeGroup);
    return group ? group.name : activeGroup;
  }

  function open(key) {
    activeGroup = (activeGroup === key) ? '' : key;
    _select();
  }

  /**
   * Back to no selection — the plain ShellMate hero (#232).
   *
   * Its own export rather than open(''): open() toggles, so calling it to
   * clear would *select* the empty key if something ever passed '' while
   * nothing was chosen. "Go home" must not depend on where you already are.
   */
  function clear() {
    activeGroup = '';
    _select();
  }

  /**
   * Redraw for a new selection, without reloading anything (#188).
   *
   * Everything a selection changes is already in memory: which chip is
   * highlighted, which tiles are painted, which tabs the strip shows. Going
   * back to the server for it made choosing a group the slowest thing in the
   * application.
   */
  function _select() {
    // Through renderWelcomeTiles rather than renderTree directly: the tiles
    // and the tree are two halves of one view of the selection, and drawing
    // one without the other is how they got out of step before.
    if (typeof window.renderWelcomeTiles === 'function') window.renderWelcomeTiles();
    if (typeof window.repaintTabGroups === 'function') window.repaintTabGroups();
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  /**
   * Load the groups and draw the tree.
   *
   * Called from renderWelcomeProfiles with the profiles it already fetched,
   * so this costs one request for the groups rather than two for everything.
   */
  /**
   * Whether the caches need refilling (#188).
   *
   * Selecting a group changes nothing on the server, but every click
   * re-fetched `/api/groups` three times and re-downloaded 1.64 MB of
   * profiles — 788ms before anything moved. The events that mean the data
   * really did change already exist and already fire; this just stops
   * pretending every click is one of them.
   */
  let groupsStale = true;
  let liveStale = true;

  window.addEventListener('shellmate:groups-changed', () => { groupsStale = true; });

  async function render(profiles) {
    if (groupsStale) {
      try {
        const res = await fetch('/api/groups');
        const data = res.ok ? await res.json() : { groups: [] };
        groupCache = data.groups || [];
        if (data.colours && data.colours.length) colours = data.colours;
        if (data.icons && data.icons.length) icons = data.icons;
        groupsStale = false;
      } catch (_) {
        groupCache = [];
      }
    }

    // A group that was open and has since been deleted must not leave the
    // dashboard filtered to nothing with no way back.
    if (activeGroup && !groupCache.some(g => g.key === activeGroup)) {
      activeGroup = '';
    }

    // Same for the multi-selection: a bulk action must never apply to a key
    // that no longer names a group.
    selected = new Set(
      [...selected].filter(k => groupCache.some(g => g.key === k)));
    if (anchor && !groupCache.some(g => g.key === anchor)) anchor = '';

    if (liveStale) await _loadLive();
    renderTree(profiles || []);
  }

  // -------------------------------------------------------------------------
  // The tree (#147)
  //
  // Replaced a wrapped row of tiles. The tiles were not broken — they were the
  // wrong shape: they grew sideways into a wall at a dozen groups, their
  // counts were too small to scan, and diving into one replaced the whole
  // view, so comparing two meant visiting them one at a time.
  //
  // A tree grows downwards, holds its shape as it fills, and shows a group's
  // contents without hiding everything else. The chip styling is kept —
  // that part was right.
  //
  // **Nesting comes from the name.** `site-3/access` is a branch under
  // `site-3`, and a group is still exactly a tag: no parent field, no
  // migration, and both names stay independently usable. If nobody nests, the
  // separator never appears and nothing was added to carry it.
  // -------------------------------------------------------------------------

  const SEPARATOR = '/';

  /** Which branches are open, by full key. */
  let expanded = new Set();

  /**
   * Multi-selection, for bulk delete and bulk edit (#groups-multi).
   *
   * Ctrl-click toggles a group in and out; shift-click takes the run between
   * the last group clicked and this one, in the order the tree paints them.
   * Selection is not the active filter: the active group decides what the
   * dashboard shows, the selection decides what a bulk action applies to,
   * and conflating them would filter the dashboard to the last of five
   * groups somebody is about to delete.
   */
  let selected = new Set();

  /** Where a shift range starts: the group chip last clicked. */
  let anchor = '';

  /** Group keys in the order the last render painted them, for shift runs. */
  let renderOrder = [];

  function _clearSelection() {
    if (!selected.size && !anchor) return;
    selected = new Set();
    anchor = '';
  }

  /** Shift-click: everything between the anchor and here, inclusive. */
  function _rangeSelect(key) {
    const from = renderOrder.indexOf(anchor);
    const to = renderOrder.indexOf(key);
    if (from === -1 || to === -1) {
      // The anchor scrolled out of existence — a collapse or a search
      // narrowed the tree since it was set. Fall back to a single pick.
      selected.add(key);
    } else {
      const [lo, hi] = from < to ? [from, to] : [to, from];
      for (let i = lo; i <= hi; i++) selected.add(renderOrder[i]);
    }
    anchor = key;
    renderTree(profileCache);
  }

  /** Connections, so a branch can list what is in it without another fetch. */
  let profileCache = [];

  function _prefs() {
    return (window.shellmateSettings || {}).interface || {};
  }

  /** Build the nested shape from flat group keys. */
  function _tree(groups) {
    const roots = [];
    const byPath = new Map();

    // Sorted so a parent is always created before its children.
    [...groups].sort((a, b) => a.key.localeCompare(b.key)).forEach(group => {
      const parts = group.key.split(SEPARATOR).filter(Boolean);
      let path = '';
      let siblings = roots;
      let parent = null;

      parts.forEach((part, depth) => {
        path = path ? `${path}${SEPARATOR}${part}` : part;
        let node = byPath.get(path);
        if (!node) {
          node = { key: path, label: part, depth, children: [],
                   group: null, parent };
          byPath.set(path, node);
          siblings.push(node);
        }
        if (depth === parts.length - 1) node.group = group;
        siblings = node.children;
        parent = node;
      });
    });

    return roots;
  }

  /**
   * What is open right now, in both the terms it can be asked about.
   *
   * `liveProfiles` is the answer where a session recorded which saved
   * connection opened it (#187). `liveKeys` remains for sessions opened
   * straight from the dialog, which have no profile to name — but it is now
   * the fallback rather than the mechanism. As the mechanism it claimed every
   * device sharing an address was connected, which across a site behind one
   * jump host is all of them.
   */
  let liveProfiles = new Set();
  let liveKeys = new Set();

  async function _loadLive() {
    try {
      const res = await fetch('/api/sessions');
      const list = res.ok ? await res.json() : [];
      const open = list.filter(s => s.is_connected);
      liveStale = false;
      liveProfiles = new Set(open.map(s => s.profile_id).filter(Boolean));
      liveKeys = new Set(open
        .filter(s => !s.profile_id)
        .map(s => `${(s.address || s.hostname || '').toLowerCase()}:${s.port || 0}`));
    } catch (_) {
      liveProfiles = new Set();
      liveKeys = new Set();
    }
  }

  /** Is this saved connection open right now? */
  function _isLive(profile) {
    if (profile.id && liveProfiles.has(profile.id)) return true;
    if (!liveKeys.size) return false;
    return liveKeys.has(
      `${(profile.hostname || '').toLowerCase()}:${profile.port || 0}`);
  }

  /**
   * How many connections in this branch are open.
   *
   * Matched on identity, not resemblance. #124 is the cautionary tale:
   * matching a session to a profile approximately is how restore connected to
   * the wrong device. Counting is harmless where connecting is not, but the
   * two should still agree about what "this device" means — and they now
   * agree on `_isLive`.
   */
  function _liveUnder(node) {
    let total = 0;
    if (node.group) {
      total += (_byTag.get(node.key) || []).filter(_isLive).length;
    }
    node.children.forEach(child => { total += _liveUnder(child); });
    return total;
  }

  /** Does this group, or a connection in it, match the search? */
  /** Whether one connection answers the current search. */
  function _profileMatches(profile) {
    if (!query) return true;
    return (profile.name || '').toLowerCase().includes(query)
        || (profile.hostname || '').toLowerCase().includes(query);
  }

  function _matches(node) {
    if (!query) return true;
    if (node.key.toLowerCase().includes(query)) return true;
    // The devices inside it too, so searching a hostname finds its group.
    return (_byTag.get(node.key) || []).some(_profileMatches);
  }

  /** Memoised per render (#358): a search render used to evaluate this
   *  three times per node — in the root filter, in mark(), and again per
   *  branch — each recursing over the whole subtree, O(n × subtree) per
   *  debounced keystroke. */
  const _matchMemo = new Map();

  /** True when this branch, or anything beneath it, matches. */
  function _branchMatches(node) {
    const cached = _matchMemo.get(node.key);
    if (cached !== undefined) return cached;
    const result = _matches(node) || node.children.some(_branchMatches);
    _matchMemo.set(node.key, result);
    return result;
  }

  /**
   * Every connection in a group, including its subgroups (#207).
   *
   * A site holds nothing directly — a device carries its subgroup's tag only
   * — so the exact-key lookup both these actions used found nothing, and the
   * menu said a site was empty while the count beside it said twelve. The
   * counts were right; the menu was asking the wrong question.
   *
   * The separator is required so `site-1` does not swallow `site-10`.
   */
  function _membersUnder(key) {
    const prefix = `${key}${SEPARATOR}`;
    const out = [];
    const seen = new Set();
    _byTag.forEach((profiles, tag) => {
      if (tag !== key && !tag.startsWith(prefix)) return;
      profiles.forEach(p => {
        // A device in two subgroups of one site is one device.
        if (seen.has(p.id)) return;
        seen.add(p.id);
        out.push(p);
      });
    });
    return out;
  }

  /**
   * The built tree, cached against the cache it was built from (#357).
   *
   * `_findNode` runs from every `renderWelcomeTiles()` — per click, per
   * sessions-changed event — and rebuilding the whole tree (a sort plus
   * full construction) for one lookup is real work at the 1,100-group
   * scale the rest of this file is engineered for. groupCache is replaced,
   * never mutated, so identity is the invalidation.
   */
  let _treeCache = null;
  let _treeCacheFor = null;

  function _builtTree() {
    if (_treeCacheFor !== groupCache || !_treeCache) {
      _treeCache = _tree(groupCache);
      _treeCacheFor = groupCache;
    }
    return _treeCache;
  }

  /** The tree node for a key, so a menu can ask about its children. */
  function _findNode(key) {
    let found = null;
    const walk = (nodes) => nodes.forEach(n => {
      if (n.key === key) found = n; else walk(n.children);
    });
    walk(_builtTree());
    return found;
  }

  /** How many groups sit beneath this one, at any depth. */
  function _descendants(node) {
    let total = node.children.length;
    node.children.forEach(child => { total += _descendants(child); });
    return total;
  }

  /** Everything in this branch, including nested groups. */
  function _countUnder(node) {
    let total = node.group ? node.group.count : 0;
    node.children.forEach(child => { total += _countUnder(child); });
    return total;
  }

  /**
   * Connections by tag, rebuilt whenever the profiles change (#188).
   *
   * `_liveUnder` and the search both used to filter every profile per node.
   * At 1,100 groups against 5,000 connections that is five and a half million
   * comparisons for one render, repeated on every click. One pass to build
   * this costs 5,000.
   */
  const _byTag = new Map();

  function _indexProfiles() {
    _byTag.clear();
    profileCache.forEach(profile => {
      (profile.tags || []).forEach(tag => {
        const bucket = _byTag.get(tag);
        if (bucket) bucket.push(profile); else _byTag.set(tag, [profile]);
      });
    });
  }

  /**
   * Connections that belong to no group the tree can show (#267).
   *
   * Two ways to end up here: no tags at all, or tags naming groups that no
   * longer exist. Both were invisible — the tree is built from groups, and
   * the home view shows only recent connections, so a newly saved connection
   * with no group appeared nowhere while still counting towards the total.
   */
  function _ungroupedProfiles() {
    const known = new Set(groupCache.map(g => g.key));
    return profileCache.filter(p =>
      !(p.tags || []).some(tag => known.has(tag)));
  }

  /** The synthetic branch holding them. Absent when there are none. */
  function _ungroupedBranch(profiles) {
    const KEY = '__ungrouped__';   // cannot collide with a real group key
    const wrap = document.createElement('div');
    wrap.className = 'tree-branch';
    wrap.style.setProperty('--depth', '0');

    const row = document.createElement('div');
    row.className = 'tree-row';

    const open = expanded.has(KEY);

    const twist = document.createElement('button');
    twist.type = 'button';
    twist.className = 'tree-twist';
    twist.innerHTML = '<span class="material-symbols-outlined">'
                    + (open ? 'keyboard_arrow_down' : 'keyboard_arrow_up')
                    + '</span>';
    twist.addEventListener('click', (e) => {
      e.stopPropagation();
      if (open) expanded.delete(KEY); else expanded.add(KEY);
      _select();
    });

    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'tree-chip group-slate tree-chip-ungrouped';
    chip.title = 'Connections that are not in any group. '
               + 'Drag one onto a group to file it.';

    const glyph = document.createElement('span');
    glyph.className = 'material-symbols-outlined tree-chip-icon';
    glyph.textContent = 'folder_open';

    const name = document.createElement('span');
    name.className = 'tree-chip-name';
    name.textContent = 'Ungrouped';

    const count = document.createElement('span');
    count.className = 'tree-chip-count';
    count.textContent = String(profiles.length);

    chip.append(glyph, name, count);
    chip.addEventListener('click', () => {
      if (open) expanded.delete(KEY); else expanded.add(KEY);
      _select();
    });

    row.append(twist, chip);
    wrap.appendChild(row);

    if (open) {
      const node = { key: KEY, depth: 0, label: 'Ungrouped', group: null };
      profiles.forEach(p => wrap.appendChild(_leaf(p, node)));
    }
    return wrap;
  }

  function renderTree(profiles) {
    const panel = document.getElementById('group-tree');
    const body = document.getElementById('group-tree-body');
    if (!panel || !body) return;

    profileCache = profiles || [];
    _indexProfiles();
    renderOrder = [];
    // Per render, not per query: the profiles a device-name search matches
    // can change between renders with the query unchanged.
    _matchMemo.clear();

    // Nothing to show is nothing to show. An empty rail taking a fifth of the
    // dashboard on a fresh install would be the tiles' mistake again.
    if (!groupCache.length) {
      panel.classList.add('hidden');
      return;
    }
    panel.classList.remove('hidden');

    const prefs = _prefs();
    const right = prefs.group_tree_side === 'right';
    const collapsed = prefs.group_tree_collapsed === true;
    // One mechanism only: `order` on the panel itself. There used to be a
    // second — row-reverse on the container — and the pair cancelled out, so
    // the dock button changed the setting and moved nothing (reported as
    // "clicking the icon with the arrows does nothing").
    panel.classList.toggle('group-tree-right', right);
    panel.classList.toggle('group-tree-collapsed', collapsed);

    // The dragged width applies only while expanded. The collapsed 44px
    // comes from a class rule, and an inline width would beat it silently —
    // a rail stuck at 300px is this bug's second act.
    const width = Number(prefs.group_tree_width) || 0;
    panel.style.width = (!collapsed && width >= 180) ? `${width}px` : '';

    // The chevron points the way it will act (#153): collapsing shrinks the
    // panel toward its own edge, expanding grows it away from it. Up/down
    // said neither.
    const collapseBtn = document.getElementById('group-tree-collapse');
    if (collapseBtn) {
      const icon = collapseBtn.querySelector('.material-symbols-outlined');
      const toward = right ? 'keyboard_arrow_right' : 'keyboard_arrow_left';
      const away = right ? 'keyboard_arrow_left' : 'keyboard_arrow_right';
      if (icon) icon.textContent = collapsed ? away : toward;
      collapseBtn.title = collapsed ? 'Show the groups' : 'Collapse';
    }

    // The collapsed rail names itself and carries the count — a 44px strip
    // with two tiny buttons read as a border, not a thing that opens.
    const rail = document.getElementById('group-tree-rail');
    if (rail) {
      rail.classList.toggle('hidden', !collapsed);
      rail.textContent = `Groups · ${groupCache.length}`;
    }

    body.innerHTML = '';

    body.appendChild(_searchBox());

    // Pinned groups first, then a divider (#162). "Add to favourites" sorted
    // them to the front and said so nowhere — at a handful of groups the
    // reordering is invisible, so the action appeared to do nothing. A rule
    // makes the effect a position rather than a subtlety, and the menu now
    // names it for what it does: "Pin to the top".
    let roots = _builtTree();
    if (query) {
      // Only branches with a match somewhere beneath them, each one forced
      // open so the match is actually reachable.
      roots = roots.filter(_branchMatches);
      matchOpen = new Set();
      const mark = (n) => {
        // Open a branch when the match is below it — in a subgroup, or in
        // one of its own connections. The second half was missing, so
        // searching a device name found its group and then hid the device
        // inside a closed branch, which is the one thing the search is for.
        if (n.children.some(_branchMatches)
            || (_byTag.get(n.key) || []).some(_profileMatches)) {
          matchOpen.add(n.key);
        }
        n.children.forEach(mark);
      };
      roots.forEach(mark);
    }
    const pinned = roots.filter(n => n.group && n.group.favourite);
    const rest = roots.filter(n => !(n.group && n.group.favourite));

    // The root (#260, renamed #295): every group is a child of "Root", so the
    // hierarchy has a visible top rather than a flat button floating over a
    // separate list. Selecting it clears the selection; the chevron folds
    // the whole tree.
    const all = document.createElement('button');
    all.type = 'button';
    all.className = 'tree-all tree-root' + (activeGroup ? '' : ' tree-active');

    const chevron = document.createElement('span');
    chevron.className = 'material-symbols-outlined tree-root-chevron';
    chevron.textContent = rootOpen ? 'keyboard_arrow_down' : 'keyboard_arrow_right';

    const rootLabel = document.createElement('span');
    // "Root" (#295) — it is the top of the tree, and naming it for what it
    // is beats naming it for what it contains.
    rootLabel.textContent = `Root (${profileCache.length})`;
    all.append(chevron, rootLabel);

    const children = document.createElement('div');
    children.className = 'tree-root-children';
    children.classList.toggle('hidden', !rootOpen);

    chevron.addEventListener('click', (e) => {
      e.stopPropagation();
      rootOpen = !rootOpen;
      chevron.textContent = rootOpen ? 'keyboard_arrow_down' : 'keyboard_arrow_right';
      children.classList.toggle('hidden', !rootOpen);
    });
    // Dropping a group here takes it out to the top level (#294) — the only
    // way back out of a parent, since every other target puts it inside
    // something.
    all.addEventListener('dragover', (e) => {
      e.preventDefault();
      all.classList.add('group-drop');
    });
    all.addEventListener('dragleave', () => all.classList.remove('group-drop'));
    all.addEventListener('drop', async (e) => {
      e.preventDefault();
      all.classList.remove('group-drop');
      const movedKey = e.dataTransfer.getData('application/x-shellmate-group');
      if (movedKey) await _reparent(movedKey, '');
    });

    all.addEventListener('click', () => {
      _clearSelection();
      // The row toggles too (#287) — aiming for a 16px chevron to fold the
      // tree is a precision the rest of the tree does not ask for.
      rootOpen = !rootOpen;
      chevron.textContent = rootOpen ? 'keyboard_arrow_down' : 'keyboard_arrow_right';
      children.classList.toggle('hidden', !rootOpen);

      activeGroup = '';
      _select();
      // Same as selecting a branch: the result must be visible (#232).
      if (typeof window.showDashboard === 'function') window.showDashboard();
    });

    pinned.forEach(node => children.appendChild(_branch(node)));
    if (pinned.length && rest.length) {
      const rule = document.createElement('div');
      rule.className = 'tree-pin-divider';
      children.appendChild(rule);
    }
    rest.forEach(node => children.appendChild(_branch(node)));

    // Last, and only when it has anything in it (#267). A connection in no
    // group had no home in the tree at all, so it was invisible while still
    // counting towards the total.
    const orphans = _ungroupedProfiles().filter(p => !query || _profileMatches(p));
    if (orphans.length) children.appendChild(_ungroupedBranch(orphans));

    body.append(all, children);

    // The dashboard's group button follows the selection (#261): at root it
    // makes a top-level group; inside one it makes a child of it. One
    // button, because the two actions are never both sensible at once.
    const newBtn = document.getElementById('btn-new-group');
    if (newBtn) {
      newBtn.innerHTML = '<span class="material-symbols-outlined">folder</span>'
        + (activeGroup ? 'New Sub-Group' : 'New Group');
    }
  }

  /** The tree's own search (#183). */
  function _searchBox() {
    const wrap = document.createElement('div');
    wrap.className = 'tree-search';

    const input = document.createElement('input');
    input.type = 'search';
    input.id = 'tree-search-input';
    input.placeholder = 'Search groups and devices';
    input.autocomplete = 'off';
    input.value = query;

    let debounce = null;
    input.addEventListener('input', () => {
      clearTimeout(debounce);
      // Matching a device means walking every profile, which at five thousand
      // is not something to do per keystroke.
      debounce = setTimeout(() => {
        query = input.value.trim().toLowerCase();
        if (!query) matchOpen = new Set();
        renderTree(profileCache);
        // The box is rebuilt by that render, so the caret has to be put back
        // or typing a second character types it somewhere else.
        const again = document.getElementById('tree-search-input');
        if (again) {
          again.focus();
          again.setSelectionRange(again.value.length, again.value.length);
        }
      }, 200);
    });

    wrap.appendChild(input);
    return wrap;
  }

  /** One branch: the group chip, its children, and its connections. */
  function _branch(node) {
    const wrap = document.createElement('div');
    wrap.className = 'tree-branch';
    wrap.style.setProperty('--depth', String(node.depth));

    const row = document.createElement('div');
    row.className = 'tree-row';

    const hasChildren = node.children.length > 0;
    const open = expanded.has(node.key) || matchOpen.has(node.key);

    const twist = document.createElement('button');
    twist.type = 'button';
    twist.className = 'tree-twist' + (hasChildren || node.group ? '' : ' tree-twist-empty');
    twist.innerHTML = '<span class="material-symbols-outlined">'
                    + (open ? 'keyboard_arrow_down' : 'keyboard_arrow_up')
                    + '</span>';
    twist.addEventListener('click', (e) => {
      e.stopPropagation();
      if (open) expanded.delete(node.key); else expanded.add(node.key);
      _select();
    });

    // The chip, kept from the tiles — that part was right.
    const chip = document.createElement('button');
    chip.type = 'button';
    const colour = node.group ? node.group.colour : 'slate';
    chip.className = `tree-chip group-${colour}`
                   + (activeGroup === node.key ? ' tree-active' : '')
                   + (node.group && selected.has(node.key) ? ' tree-selected' : '')
                   + (node.group && node.group.favourite ? ' group-favourite' : '');
    chip.dataset.key = node.key;

    // Only real groups take part in shift runs — a bare path segment has
    // nothing a bulk action could apply to.
    if (node.group) renderOrder.push(node.key);

    // The group's face (#180). A branch with no group of its own — a path
    // segment that exists only because something below it is named after it —
    // gets the plain folder, which is what it is.
    const glyph = document.createElement('span');
    glyph.className = 'material-symbols-outlined tree-chip-icon';
    glyph.textContent = (node.group && node.group.icon) || 'folder';

    const name = document.createElement('span');
    name.className = 'tree-chip-name';
    name.textContent = node.group ? node.group.name.split(SEPARATOR).pop() : node.label;

    const count = document.createElement('span');
    count.className = 'tree-chip-count';
    count.textContent = _countUnder(node);

    // How many are open right now, which is the more useful number in the
    // moment (#146). Shown only when it is not zero — a "0 live" on every
    // group would be noise on a dashboard opened before connecting to
    // anything.
    const live = _liveUnder(node);
    // The chip itself, not only its badge (#198). A count is a number to
    // read; the group looking live is something to notice. It aggregates,
    // so one open device marks its subgroup and its site — which is right
    // here, and was the bug when it happened by accident across unrelated
    // sites (#187).
    // Not `tree-chip-live` — that is already the badge's class, and putting it
    // on the chip too would style the chip as a badge.
    chip.classList.toggle('tree-chip-has-live', Boolean(live));
    if (live) {
      const badge = document.createElement('span');
      badge.className = 'tree-chip-live';
      badge.textContent = live;
      badge.title = `${live} open now`;
      chip.appendChild(badge);
    }

    chip.append(glyph, name, count);
    chip.addEventListener('click', (e) => {
      // Ctrl and shift build a multi-selection instead of navigating —
      // the tree must not fold, filter or change view under a gesture whose
      // whole point is picking several things before acting on them.
      if (node.group && (e.ctrlKey || e.metaKey)) {
        if (selected.has(node.key)) selected.delete(node.key);
        else selected.add(node.key);
        anchor = node.key;
        renderTree(profileCache);
        return;
      }
      if (node.group && e.shiftKey && anchor) {
        _rangeSelect(node.key);
        return;
      }
      _clearSelection();

      // Clicking opens and closes it (#200), and selects it.
      //
      // It used to toggle the *selection* and only ever expand, so a second
      // click on the same group deselected it while leaving it open — which
      // read as the click doing nothing. Selection cannot simply move to the
      // twist arrow: it decides which tiles are painted (#177) and what the
      // hero names (#179), so a click does both and the root row stays
      // the way to clear it. Collapsing and emptying the dashboard on one
      // click would be two answers to one gesture.
      if (expanded.has(node.key)) expanded.delete(node.key);
      else expanded.add(node.key);
      activeGroup = node.key;
      if (node.group) anchor = node.key;
      _select();
      // The dashboard has to come forward to show what was just selected —
      // with a terminal on screen, the tiles were painted into a hidden
      // layer and the click read as doing nothing (#232).
      if (typeof window.showDashboard === 'function') window.showDashboard();
    });

    // Right-click gets the same menu the tune button opens (#170). Members
    // gained one for #167 and groups did not, so the discoverable gesture did
    // nothing on the thing that most looks like it should respond to it.
    //
    // stopPropagation as well as preventDefault: the chip's own click selects
    // the group, and without it a right-click would also filter the dashboard.
    chip.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (node.group) _tileMenu(e, node.group);
    });

    // A group tile could be dropped onto; so can a branch.
    _bindTreeDrop(chip, node);

    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'tree-more';
    more.title = 'Rename, recolour or delete';
    more.innerHTML = '<span class="material-symbols-outlined">tune</span>';
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      if (node.group) _tileMenu(e, node.group);
    });

    row.append(twist, chip);
    if (node.group) row.appendChild(more);
    wrap.appendChild(row);

    if (!open) return wrap;

    node.children.filter(c => !query || _branchMatches(c))
                 .forEach(child => wrap.appendChild(_branch(child)));

    // The connections themselves, which is the half a tile could never show.
    // Narrowed to the matches while searching, or a hit in a group of fifty
    // arrives with the forty-nine it is not.
    if (node.group) {
      (_byTag.get(node.key) || [])
        .filter(_profileMatches)
        .forEach(profile => wrap.appendChild(_leaf(profile, node)));
    }
    return wrap;
  }

  /** A connection under a group. Clicking it connects. */
  function _leaf(profile, node) {
    const leaf = document.createElement('button');
    leaf.type = 'button';
    leaf.className = 'tree-leaf';
    leaf.style.setProperty('--depth', String(node.depth + 1));
    leaf.title = profile.hostname || '';

    // Whether this device is open right now (#166). The same test the group
    // counts use, so a member's light and its group's live badge cannot
    // disagree about what "this device" means.
    const live = _isLive(profile);

    const dot = document.createElement('span');
    dot.className = 'tree-leaf-dot' + (live ? ' tree-leaf-live' : '');
    dot.title = live ? 'Connected' : 'Not open';

    const label = document.createElement('span');
    label.className = 'tree-leaf-name';
    label.textContent = profile.name || profile.hostname || '';

    leaf.append(dot, label);

    if (profile.has_saved_credentials) {
      const lock = document.createElement('span');
      lock.className = 'material-symbols-outlined tree-leaf-lock';
      lock.textContent = 'key';
      lock.title = 'A saved credential is stored for this connection';
      leaf.appendChild(lock);
    }
    leaf.addEventListener('click', () => {
      if (typeof window.connectProfile === 'function') window.connectProfile(profile);
      else if (typeof window.showConnectionDialog === 'function') {
        window.showConnectionDialog(profile);
      }
    });
    leaf.addEventListener('contextmenu', (e) => _memberMenu(e, profile, node));
    return leaf;
  }

  /**
   * What can be done to a connection from inside a group (#167).
   *
   * Move and copy are genuinely different here rather than one operation
   * with a flag: membership is overlapping, so copying adds a tag and moving
   * adds one *and takes away the one it was listed under*. Move is the first
   * thing in this feature that removes a connection from a group, so it says
   * so plainly.
   */
  function _memberMenu(event, profile, node) {
    event.preventDefault();
    event.stopPropagation();
    document.querySelectorAll('.group-menu').forEach(el => el.remove());

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu group-menu';

    const item = (icon, text, onClick, danger) => {
      const button = document.createElement('button');
      button.type = 'button';
      if (danger) button.className = 'ctx-danger';
      button.innerHTML = '<span class="material-symbols-outlined">' + icon + '</span>';
      button.appendChild(document.createTextNode(text));
      button.addEventListener('click', () => { menu.remove(); onClick(); });
      return button;
    };

    menu.appendChild(item('content_copy', 'Copy to group...',
                          () => _moveMember(profile, node, false)));
    menu.appendChild(item('tab_duplicate', 'Move to group...',
                          () => _moveMember(profile, node, true)));

    const sep = document.createElement('div');
    sep.className = 'ctx-sep';
    menu.appendChild(sep);

    menu.appendChild(item('backspace', 'Remove from "' + node.label + '"',
                          () => _removeMember(profile, node)));
    // Named unmistakably. In a tree of groups "delete" reads as "remove from
    // this group", and it would actually delete the saved connection, which
    // is not undoable.
    menu.appendChild(item('delete_forever', 'Delete this connection...',
                          () => _deleteMember(profile), true));

    document.body.appendChild(menu);
    // Clamped like the group menu (#264) — a member at the bottom of the
    // tree deserves a reachable menu too.
    menu.style.left = `${Math.max(8, Math.min(event.clientX,
      window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(event.clientY,
      window.innerHeight - menu.offsetHeight - 8))}px`;
    setTimeout(() => {
      document.addEventListener('click', () => menu.remove(), { once: true });
    }, 0);
  }

  async function _moveMember(profile, node, removeFromCurrent) {
    const choices = groupCache
      .filter(g => g.key !== node.key)
      .map(g => ({ value: g.key, label: g.name }));
    if (!choices.length) {
      _warn('No other group to use', 'Make another group first.');
      return;
    }

    const answer = await window.shellmateDialog.form({
      title: (removeFromCurrent ? 'Move ' : 'Copy ') + profile.name,
      body:  removeFromCurrent
        ? 'It leaves "' + node.label + '" and joins the group you pick.'
        : 'It joins the group you pick and stays in "' + node.label
          + '" - a connection belongs to as many groups as you like.',
      confirmLabel: removeFromCurrent ? 'Move it' : 'Copy it',
      fields: [{ name: 'group', label: 'Group', type: 'select', options: choices }],
    });
    if (!answer) return;

    await _setMembership(profile.id, answer.group, true);
    if (removeFromCurrent) await _setMembership(profile.id, node.key, false);
    _refresh();
  }

  async function _removeMember(profile, node) {
    await _setMembership(profile.id, node.key, false);
    _refresh();
  }

  async function _deleteMember(profile) {
    const ok = await window.shellmateDialog.confirm({
      title: 'Delete the connection "' + profile.name + '"?',
      body:  'This deletes the saved connection itself, not just its place in '
             + 'this group. Any credentials saved against it go with it, and it '
             + 'cannot be undone.',
      confirmLabel: 'Delete it',
      danger: true,
    });
    if (!ok) return;
    try {
      await fetch('/api/profiles/' + profile.id, { method: 'DELETE' });
      _refresh();
    } catch (e) {
      _warn('Could not delete it', e.message);
    }
  }

  async function _setMembership(profileId, key, member) {
    try {
      await fetch('/api/groups/' + encodeURIComponent(key) + '/members', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ profile_id: profileId, member }),
      });
      // Membership changed, not just presentation — the tab strip caches
      // which group each open session belongs to and has no other way to
      // learn it has moved (#169).
      window.dispatchEvent(new CustomEvent('shellmate:groups-changed'));
    } catch (_) { /* the refresh showing nothing changed is the report */ }
  }

  function _bindTreeDrop(chip, node) {
    // A branch can be picked up as well as dropped onto (#294). Its own
    // MIME type, so a group being dragged and a connection being dragged
    // cannot be mistaken for one another.
    if (node.group) {
      chip.draggable = true;
      chip.addEventListener('dragstart', (e) => {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('application/x-shellmate-group', node.key);
        chip.classList.add('group-dragging');
      });
      chip.addEventListener('dragend', () => chip.classList.remove('group-dragging'));
    }

    chip.addEventListener('dragover', (e) => {
      e.preventDefault();
      chip.classList.add('group-drop');
    });
    chip.addEventListener('dragleave', () => chip.classList.remove('group-drop'));
    chip.addEventListener('drop', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      chip.classList.remove('group-drop');

      const profileId = e.dataTransfer.getData('application/x-shellmate-profile');
      if (profileId && node.group) { await _addMember(node.group, profileId); return; }

      const movedKey = e.dataTransfer.getData('application/x-shellmate-group');
      if (movedKey && node.group) await _reparent(movedKey, node.key);
    });
  }

  /**
   * Move a group under another, or out to the top (#294).
   *
   * Nesting is the name — `glasgow/switches` is a branch under `glasgow` —
   * so re-parenting is a rename, and renaming a group re-tags every
   * connection in it. The backend already does that for a rename, and does
   * it for the subgroups too; this only has to work out the new name and
   * refuse the moves that make no sense.
   */
  async function _reparent(movedKey, targetKey) {
    if (!movedKey || movedKey === targetKey) return;

    const moved = groupCache.find(g => g.key === movedKey);
    if (!moved) return;

    // Into itself or into its own descendant: the group would become its own
    // ancestor, and every connection in it would be renamed into a path that
    // no longer has a top.
    if (targetKey && (targetKey === movedKey
                      || targetKey.startsWith(`${movedKey}${SEPARATOR}`))) {
      _warn('That would put a group inside itself',
            `"${moved.name}" cannot become a subgroup of something beneath it.`);
      return;
    }

    const leaf = moved.name.split(SEPARATOR).pop();
    const target = targetKey ? groupCache.find(g => g.key === targetKey) : null;
    const newName = target ? `${target.name}${SEPARATOR}${leaf}` : leaf;
    if (newName === moved.name) return;

    if (groupCache.some(g => g.key !== movedKey && g.name === newName)) {
      _warn('There is already a group there',
            `"${newName}" exists. Rename one of them first.`);
      return;
    }

    const members = _membersUnder(movedKey).length;
    const ok = await window.shellmateDialog.confirm({
      title: target ? `Move "${leaf}" into "${target.name}"?`
                    : `Move "${leaf}" to the top level?`,
      body:  `It becomes "${newName}". Every connection in it, and in any `
             + 'group beneath it, is re-tagged to match — nothing leaves a '
             + 'group and no session is touched.',
      list:  members ? [{ text: `${members} connection${members === 1 ? '' : 's'} affected` }] : [],
      confirmLabel: 'Move it',
    });
    if (!ok) return;

    try {
      const res = await fetch('/api/groups/' + encodeURIComponent(movedKey), {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ name: newName }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not move it.');
      // The server's key, not the display name (#349): keys are lowercased,
      // and booking "Glasgow/Switches" into sets compared against
      // "glasgow/switches" left the moved branch closed and silently
      // cleared the active filter.
      const movedGroup = await res.json();
      // The moved branch is where somebody will look next.
      expanded.add(movedGroup.key || newName);
      if (target) expanded.add(target.key);
      if (activeGroup === movedKey) activeGroup = movedGroup.key || newName;
      _refresh();
    } catch (e) {
      _warn('Could not move the group', e.message);
    }
  }

  // -------------------------------------------------------------------------
  // Docking and collapsing
  // -------------------------------------------------------------------------

  /**
   * Drag the tree's inner edge to resize it (#153).
   *
   * The same shape as panel_resize.js — clamp, persist on mouse-up so a drag
   * is one write rather than one per frame, double-click to reset — but not
   * that module: it is hardwired to right-anchored overlay panels, and this
   * panel is docked left or right by preference, so the drag direction flips
   * with the side.
   */
  /**
   * Re-measure every terminal after the tree changes width (#196).
   *
   * The tree is a column of the terminal pane now, so collapsing it, docking
   * it or dragging it makes the terminals wider or narrower. xterm sizes
   * itself in whole character cells against its container and does not watch
   * for that, so without this a terminal keeps the geometry it had and wraps
   * at the wrong column — and the far end goes on being told the old size,
   * which is the same class of failure as #143.
   *
   * After a frame, because the width transition has to have run for the
   * container to have its new size to measure against.
   */
  function _refitTerminals() {
    setTimeout(() => {
      if (typeof window.refitTerminals !== 'function') return;
      // Only what is on screen. Fitting a display:none container measures
      // zero and would set the terminal to a nonsense size, which is worse
      // than leaving it to be fitted when it is next shown.
      const visible = (window.shellmateLayout && window.shellmateLayout.visible)
        ? window.shellmateLayout.visible()
        : [];
      const active = (typeof window.getActiveTab === 'function')
        ? window.getActiveTab() : null;
      const ids = visible.length
        ? visible
        : (active ? [active.sessionId] : []);
      if (ids.length) window.refitTerminals(ids);
    }, 220);   // after the panel's own width transition
  }

  function _bindResize() {
    const handle = document.getElementById('group-tree-handle');
    const panel = document.getElementById('group-tree');
    if (!handle || !panel) return;

    const MIN = 180;
    const MAX = 520;
    let startX = 0;
    let startWidth = 0;
    // What the drag decided. Persisted from here rather than re-measured at
    // mouse-up: a re-render between the last move and the release resets the
    // inline width, and the measurement then saves the width it had *before*
    // the drag — which is how a 400px drag stored 260.
    let dragged = 0;

    const onMove = (e) => {
      const right = _prefs().group_tree_side === 'right';
      const delta = right ? (startX - e.clientX) : (e.clientX - startX);
      dragged = Math.round(Math.min(Math.max(startWidth + delta, MIN), MAX));
      panel.style.width = `${dragged}px`;
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.classList.remove('resizing-panel');
      if (dragged && window.shellmatePrefs) {
        window.shellmatePrefs.set('group_tree_width', dragged);
      }
      // On release, not per mousemove: a refit reflows the buffer and tells
      // the device, and doing that sixty times a second during a drag would
      // send the far end a resize per frame.
      _refitTerminals();
    };

    handle.addEventListener('mousedown', (e) => {
      if (_prefs().group_tree_collapsed === true) return;
      e.preventDefault();
      e.stopPropagation();
      startX = e.clientX;
      startWidth = panel.getBoundingClientRect().width;
      dragged = 0;
      document.body.classList.add('resizing-panel');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Double-click goes back to the stylesheet width — the way out of having
    // dragged it somewhere useless.
    handle.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      panel.style.width = '';
      if (window.shellmatePrefs) window.shellmatePrefs.set('group_tree_width', 0);
      _refitTerminals();
    });
  }

  function _bindPanelControls() {
    // Anywhere on the collapsed rail expands it — not only the chevron.
    // Somebody who did not knowingly collapse it should not need to find
    // one 16px button to undo it.
    const panel = document.getElementById('group-tree');
    if (panel) panel.addEventListener('click', () => {
      if (_prefs().group_tree_collapsed === true) {
        if (window.shellmatePrefs) window.shellmatePrefs.set('group_tree_collapsed', false);
        renderTree(profileCache);
        _refitTerminals();
      }
    });

    _bindResize();

    const dock = document.getElementById('group-tree-dock');
    if (dock) dock.addEventListener('click', (e) => {
      e.stopPropagation();
      const side = _prefs().group_tree_side === 'right' ? 'left' : 'right';
      if (window.shellmatePrefs) window.shellmatePrefs.set('group_tree_side', side);
      renderTree(profileCache);
      _refitTerminals();
    });

    const collapse = document.getElementById('group-tree-collapse');
    if (collapse) collapse.addEventListener('click', (e) => {
      e.stopPropagation();
      const now = !(_prefs().group_tree_collapsed === true);
      if (window.shellmatePrefs) window.shellmatePrefs.set('group_tree_collapsed', now);
      renderTree(profileCache);
      _refitTerminals();
    });
  }

  function _expandAll(node, open) {
    const walk = (n) => {
      if (open) expanded.add(n.key); else expanded.delete(n.key);
      n.children.forEach(walk);
    };
    walk(node);
    renderTree(profileCache);
  }

  /**
   * Open every connection in a group (#181).
   *
   * Through connectTag, which paces the handshakes and reports per device
   * rather than skipping failures silently. It confirms with a count, which
   * matters more here than on a tile: a subgroup is five devices, a site is
   * fifty.
   */
  function _connectAll(group) {
    const count = _membersUnder(group.key).length;
    if (!count) {
      _warn('Nothing to connect', `"${group.name}" has no connections in it.`);
      return;
    }
    if (typeof window.connectTag === 'function') {
      window.connectTag(group.key, count);
    }
  }

  /**
   * Close every open session belonging to a group.
   *
   * Confirmed, and the count named, because this drops live sessions — the
   * one thing the interface is otherwise careful never to do by accident.
   * Closing the window only hides it for exactly that reason.
   */
  async function _disconnectAll(group) {
    // The same address:port match the live badges use, so what gets closed
    // and what was counted agree about what "this device" means.
    const members = _membersUnder(group.key);
    const ids = new Set(members.map(p => p.id));
    const keys = new Set(members.map(
      p => `${(p.hostname || '').toLowerCase()}:${p.port || 0}`));

    const tabs = (typeof window.getOpenTabs === 'function' ? window.getOpenTabs() : [])
      .filter(tab => (tab.profileId
        ? ids.has(tab.profileId)
        : keys.has(`${(tab.hostname || '').toLowerCase()}:${tab.port || 0}`)));

    if (!tabs.length) {
      _warn('Nothing to disconnect', `No open sessions from "${group.name}".`);
      return;
    }

    const ok = await window.shellmateDialog.confirm({
      title: `Disconnect ${tabs.length} session${tabs.length === 1 ? '' : 's'} `
             + `from "${group.name}"?`,
      body:  'They close on the device as well as here, and anything running '
             + 'in them stops.',
      confirmLabel: 'Disconnect them',
      danger: true,
    });
    if (!ok) return;

    for (const tab of tabs) {
      if (typeof window.closeTabBySessionId === 'function') {
        // force: the count above was the confirmation. Asking again per tab
        // would be fifty dialogs for one decision already taken.
        await window.closeTabBySessionId(tab.sessionId, { force: true });
      }
    }
  }

  // -------------------------------------------------------------------------
  // Creating and editing
  // -------------------------------------------------------------------------

  /**
   * The icon grid, shared by creation and the later edit (#259).
   *
   * Returns the element and a reader for the choice, so each dialog can
   * embed it wherever it belongs.
   */
  function _iconGrid(initial) {
    const grid = document.createElement('div');
    grid.className = 'icon-picker';
    let chosen = initial;

    icons.forEach(name => {
      const cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'icon-picker-cell' + (name === initial ? ' chosen' : '');
      cell.title = name.replace(/_/g, ' ');
      cell.innerHTML = '<span class="material-symbols-outlined">' + name + '</span>';
      cell.addEventListener('click', () => {
        chosen = name;
        grid.querySelectorAll('.icon-picker-cell')
            .forEach(c => c.classList.remove('chosen'));
        cell.classList.add('chosen');
      });
      grid.appendChild(cell);
    });

    return { el: grid, chosen: () => chosen };
  }

  /** The default for a new group (#259): a site, because that is what most
   *  top-level groups are. Falls back when the fetched list lacks it. */
  function _defaultIcon() {
    return icons.includes('apartment') ? 'apartment' : 'folder';
  }

  async function newGroup() {
    const picker = _iconGrid(_defaultIcon());
    const answer = await window.shellmateDialog.form({
      title: 'New group',
      body:  'Groups arrange the dashboard. A connection can be in as many as '
             + 'you like — adding it to one never takes it out of another.',
      confirmLabel: 'Create',
      // The icon up front (#259), not a second trip through the menu.
      content: picker.el,
      fields: [
        { name: 'name', label: 'Name', required: true, placeholder: 'Glasgow',
          hint: 'If you already tag connections with this name, they join it.' },
        { name: 'colour', label: 'Colour', type: 'select',
          // Greyed when group colours are switched off (#293) — the choice
          // would be saved and never seen.
          disabled: !_coloursOn(),
          hint: _coloursOn() ? '' : 'Group colours are switched off in Settings.',
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
      ],
    });
    if (!answer) return;

    try {
      const res = await fetch('/api/groups', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ ...answer, icon: picker.chosen() }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not create it.');
      const group = await res.json();
      // Belt and braces: if creation ignored the icon, set it explicitly.
      if (picker.chosen() && (group.icon || 'folder') !== picker.chosen()) {
        await fetch('/api/groups/' + encodeURIComponent(group.key), {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ icon: picker.chosen() }),
        }).catch(() => { /* the group exists; the icon is cosmetic */ });
      }
      // Selected and expanded, because the next thing anybody does after
      // making a group is put something in it — and unlike the tiles this
      // does not replace the view to do it.
      activeGroup = group.key;
      expanded.add(group.key);
      _refresh();
    } catch (e) {
      _warn('Could not create the group', e.message);
    }
  }

  /**
   * A group inside another (#163).
   *
   * The parent is prefilled and the separator applied here, so nobody has to
   * know the convention in order to use it.
   */
  async function newSubgroup(parent) {
    // The parent's icon by default (#259): subgroups usually read as parts
    // of the same thing, and a different icon is one click away.
    const picker = _iconGrid(parent.icon || _defaultIcon());
    const answer = await window.shellmateDialog.form({
      title: 'New group inside "' + parent.name + '"',
      body:  'It appears as a branch under the parent, and the parent counts '
             + 'everything beneath it.',
      confirmLabel: 'Create',
      content: picker.el,
      fields: [
        { name: 'name', label: 'Name', required: true, placeholder: 'access',
          hint: 'Stored as "' + parent.name + '/...", which is what makes it nest.' },
        { name: 'colour', label: 'Colour', type: 'select',
          // Greyed when group colours are switched off (#293) — the choice
          // would be saved and never seen.
          disabled: !_coloursOn(),
          hint: _coloursOn() ? '' : 'Group colours are switched off in Settings.',
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
      ],
    });
    if (!answer) return;

    try {
      const res = await fetch('/api/groups', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ name: parent.name + '/' + answer.name,
                                  colour: answer.colour,
                                  icon: picker.chosen() }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not create it.');
      const group = await res.json();
      if (picker.chosen() && (group.icon || 'folder') !== picker.chosen()) {
        await fetch('/api/groups/' + encodeURIComponent(group.key), {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ icon: picker.chosen() }),
        }).catch(() => { /* the group exists; the icon is cosmetic */ });
      }
      expanded.add(parent.key);
      activeGroup = group.key;
      _refresh();
    } catch (e) {
      _warn('Could not create the subgroup', e.message);
    }
  }

  /**
   * Choose a group's icon (#180).
   *
   * A grid of the ones that ship, not a text field. The font carries only
   * what the source names, so anything typed would be a coin toss between an
   * icon and the word itself printed on the dashboard.
   */
  async function pickIcon(group) {
    // The same grid the creation dialogs embed (#259) — one picker, not two
    // that could drift.
    const picker = _iconGrid(group.icon || 'folder');

    const ok = await window.shellmateDialog.confirm({
      title: 'Icon for "' + group.name + '"',
      body:  'It appears beside the name in the tree and on the tab strip.',
      confirmLabel: 'Use it',
      content: picker.el,
    });
    if (!ok) return;

    try {
      const res = await fetch('/api/groups/' + encodeURIComponent(group.key), {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ icon: picker.chosen() }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not save it.');
      _refresh();
    } catch (e) {
      _warn('Could not change the icon', e.message);
    }
  }

  async function editGroup(group) {
    const answer = await window.shellmateDialog.form({
      title: `Edit "${group.name}"`,
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Name', value: group.name, required: true,
          hint: 'Renaming re-tags every connection in the group.' },
        { name: 'colour', label: 'Colour', type: 'select', value: group.colour,
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
        { name: 'favourite', label: 'Pinned to the top', type: 'checkbox',
          value: group.favourite,
          hint: 'Pinned groups are listed first, above a divider.' },
      ],
    });
    if (!answer) return;
    await _update(group.key, answer);
  }

  async function deleteGroup(group) {
    // Named for what survives. Nothing else on the dashboard destroys
    // anything, and "delete group" reads like it takes the devices with it.
    //
    // The counts are the subtree's, not the group's own: a site holds nothing
    // directly, so `group.count` said "empty" while twelve devices sat in its
    // subgroups — and the subgroups go with it now, which the dialog says.
    const node = _findNode(group.key);
    const subgroups = node ? _descendants(node) : 0;
    const members = _membersUnder(group.key);
    const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

    // Two readings of "delete the group" (#360), offered as a choice rather
    // than decided here. Keeping the connections drops any that lived only
    // in this group into Ungrouped, which reads as the delete not having
    // worked; deleting them takes saved credentials with them and cannot be
    // undone. Neither is right for everyone, so the dialog says what each
    // does and keeps the reversible one selected.
    //
    // Only the connections that would land in Ungrouped are offered for
    // deletion. One that also belongs to another group is evidently still
    // wanted there — the server applies the same rule from the file, so this
    // count is for the wording, not the decision.
    const prefix = `${group.key}${SEPARATOR}`;
    const inHere = tag => tag === group.key || tag.startsWith(prefix);
    const orphans = members.filter(p => (p.tags || []).every(inHere)).length;
    const shared = members.length - orphans;

    let choice = null;
    let body;
    if (!members.length) {
      body = subgroups
        ? `It and its ${plural(subgroups, 'subgroup')} are empty, so nothing else changes.`
        : 'It is empty, so nothing else changes.';
    } else {
      body = 'Only the group'
        + (subgroups ? `, its ${plural(subgroups, 'subgroup')}` : '')
        + ` and its colour go. Its ${plural(members.length, 'connection')}:`;
      const all = orphans === members.length;
      choice = _deleteChoice({
        keep: !orphans
          ? 'Keep them where they are — they all belong to other groups too.'
          : all
            ? 'Keep them — they move to Ungrouped.'
            : `Keep them — ${orphans} move to Ungrouped, ${shared} `
              + `${shared === 1 ? 'stays in its' : 'stay in their'} other groups.`,
        remove: orphans
          ? `Also delete ${all ? 'them' : `the ${orphans} that are only in this group`}`
            + ' — the saved connections and any credentials go, and cannot be recovered.'
          : '',
      });
    }

    const KEEP_LABEL = 'Delete the group';
    const REMOVE_LABEL = 'Delete group and connections';
    if (choice) {
      choice.onChange = (removing) => {
        // The button says what will happen; the label is what a hurried
        // reader looks at, and "Delete the group" must not be what they see
        // while twenty connections are about to go with it.
        const box = choice.el.closest('.sm-dialog');
        const button = box && box.querySelector('.sm-dialog-actions .btn-danger');
        if (button) button.textContent = removing ? REMOVE_LABEL : KEEP_LABEL;
      };
    }

    const ok = await window.shellmateDialog.confirm({
      title: `Delete the group "${group.name}"?`,
      body,
      content: choice ? choice.el : undefined,
      confirmLabel: KEEP_LABEL,
      danger: true,
    });
    if (!ok) return;
    const removing = Boolean(choice && choice.removing());

    try {
      const query = removing ? '?connections=delete' : '';
      const res = await fetch(`/api/groups/${encodeURIComponent(group.key)}${query}`,
                              { method: 'DELETE' });
      if (!res.ok) throw new Error('Could not delete it.');
      const data = await res.json();
      if (activeGroup === group.key) activeGroup = '';
      _refresh();
      if (window.shellmateAlerts && (data.released || data.deleted)) {
        const parts = [];
        if (data.deleted)  parts.push(`${plural(data.deleted, 'connection')} deleted`);
        if (data.released) parts.push(`${plural(data.released, 'connection')} kept`);
        window.shellmateAlerts.notify({
          title: `Group "${group.name}" deleted`,
          body:  parts.join(', ') + '.',
        });
      }
    } catch (e) {
      _warn('Could not delete the group', e.message);
    }
  }

  /**
   * The keep-or-delete choice for deleteGroup, as radios rather than a
   * checkbox: both outcomes get a sentence, and neither is a modifier on the
   * other. "Keep" is selected first, because it is the one that can be undone.
   *
   * @param {{keep: string, remove: string}} labels — an empty `remove`
   *        (nothing would be orphaned) renders the keep row alone, so the
   *        dialog still says where the connections go.
   * @returns {{el: Element, removing: () => boolean, onChange: Function}}
   */
  function _deleteChoice(labels) {
    const el = document.createElement('div');
    el.className = 'sm-dialog-choice';
    const api = { el, removing: () => false, onChange: null };

    const row = (value, text, checked) => {
      const label = document.createElement('label');
      label.className = 'sm-dialog-choice-row';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'sm-dialog-group-delete';
      radio.value = value;
      radio.checked = checked;
      radio.addEventListener('change', () => {
        if (radio.checked && typeof api.onChange === 'function') {
          api.onChange(value === 'delete');
        }
      });
      const span = document.createElement('span');
      span.textContent = text;   // a group name can reach this
      label.append(radio, span);
      el.appendChild(label);
      return radio;
    };

    row('keep', labels.keep, true);
    if (labels.remove) {
      const remove = row('delete', labels.remove, false);
      api.removing = () => remove.checked;
    }
    return api;
  }

  async function _update(key, changes) {
    try {
      const res = await fetch(`/api/groups/${encodeURIComponent(key)}`, {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(changes),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not save it.');
      const group = await res.json();
      // A rename changes the key, so anything open follows it.
      if (activeGroup === key) activeGroup = group.key;
      _refresh();
    } catch (e) {
      _warn('Could not update the group', e.message);
    }
  }

  /**
   * The tile's own menu.
   *
   * Reuses `.tab-context-menu`, which is already styled and already handles
   * the dismissal rules — a second menu that looked almost like the first
   * would be the kind of drift the interface has been kept free of.
   */
  function _tileMenu(event, group) {
    // A menu opened on one of several selected groups speaks for all of
    // them — that is what selecting several was for. On a group outside the
    // selection it behaves as ever, for just that group.
    if (selected.size > 1 && selected.has(group.key)) {
      _bulkMenu(event);
      return;
    }
    document.querySelectorAll('.group-menu').forEach(el => el.remove());

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu group-menu';

    const item = (icon, text, onClick, danger, disabled) => {
      const button = document.createElement('button');
      button.type = 'button';
      if (danger) button.className = 'ctx-danger';
      button.innerHTML = `<span class="material-symbols-outlined">${icon}</span>`;
      button.appendChild(document.createTextNode(text));
      if (disabled) {
        // Present but grey (#262): an entry that vanishes leaves someone
        // wondering where it went; one that explains itself does not.
        button.disabled = true;
        button.title = 'The group has no connections in it.';
      } else {
        button.addEventListener('click', () => { menu.remove(); onClick(); });
      }
      return button;
    };

    menu.appendChild(item('tune', 'Rename or recolour…', () => editGroup(group)));
    menu.appendChild(item('palette', 'Change the icon...', () => pickIcon(group)));
    // Nesting was already rendered - a group named "site-3/access" becomes
    // a branch under "site-3" - but the only way to make one was typing the
    // separator by hand into the name field, which nothing explained (#163).
    menu.appendChild(item('add', 'New subgroup...', () => newSubgroup(group)));
    // Straight into this group (#263): the dialog opens with the Groups
    // field prefilled, so the new profile lands where the click was.
    menu.appendChild(item('add_circle', 'New connection...', () => {
      if (typeof window.showConnectionDialog === 'function') {
        window.showConnectionDialog({ connection_type: 'ssh', tags: [group.key] });
      }
    }));

    // At 100 sites of 10 subgroups, opening a tree by hand is not a thing
    // anybody is going to do (#182).
    const node = _findNode(group.key);
    if (node && node.children.length) {
      menu.appendChild(item('unfold_more', 'Expand all below',
                            () => _expandAll(node, true)));
      menu.appendChild(item('unfold_less', 'Collapse all below',
                            () => _expandAll(node, false)));
    }

    const sweep = document.createElement('div');
    sweep.className = 'ctx-sep';
    menu.appendChild(sweep);
    // Grey for an empty group (#262) — the actions could only ever say
    // "nothing to connect", and a menu entry that always fails is a trap.
    const empty = _membersUnder(group.key).length === 0;
    menu.appendChild(item('add_circle', 'Connect all',
                          () => _connectAll(group), false, empty));
    menu.appendChild(item('stop_circle', 'Disconnect all',
                          () => _disconnectAll(group), true, empty));
    menu.appendChild(item(
      'bookmark_add',
      group.favourite ? 'Unpin from the top' : 'Pin to the top',
      () => _update(group.key, { favourite: !group.favourite })));

    const sep = document.createElement('div');
    sep.className = 'ctx-sep';
    menu.appendChild(sep);

    menu.appendChild(item('delete_forever', 'Delete group…',
                          () => deleteGroup(group), true));

    document.body.appendChild(menu);
    // Clamped on both axes (#264): a group near the bottom of a full tree
    // used to open its menu half off screen.
    menu.style.left = `${Math.max(8, Math.min(event.clientX,
      window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(event.clientY,
      window.innerHeight - menu.offsetHeight - 8))}px`;

    setTimeout(() => {
      document.addEventListener('click', () => menu.remove(), { once: true });
    }, 0);
  }

  // -------------------------------------------------------------------------
  // Bulk actions (#groups-multi)
  // -------------------------------------------------------------------------

  /** The menu a multi-selection gets: act on all of them, or let them go. */
  function _bulkMenu(event) {
    document.querySelectorAll('.group-menu').forEach(el => el.remove());

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu group-menu';
    const n = selected.size;

    const item = (icon, text, onClick, danger) => {
      const button = document.createElement('button');
      button.type = 'button';
      if (danger) button.className = 'ctx-danger';
      button.innerHTML = `<span class="material-symbols-outlined">${icon}</span>`;
      button.appendChild(document.createTextNode(text));
      button.addEventListener('click', () => { menu.remove(); onClick(); });
      return button;
    };

    menu.appendChild(item('tune', `Edit ${n} groups…`, _bulkEdit));

    const sep = document.createElement('div');
    sep.className = 'ctx-sep';
    menu.appendChild(sep);

    menu.appendChild(item('delete_forever', `Delete ${n} groups…`,
                          _bulkDelete, true));
    menu.appendChild(item('close', 'Clear selection', () => {
      _clearSelection();
      renderTree(profileCache);
    }));

    document.body.appendChild(menu);
    menu.style.left = `${Math.max(8, Math.min(event.clientX,
      window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(event.clientY,
      window.innerHeight - menu.offsetHeight - 8))}px`;
    setTimeout(() => {
      document.addEventListener('click', () => menu.remove(), { once: true });
    }, 0);
  }

  /** Recolour or pin every selected group in one pass. */
  async function _bulkEdit() {
    const keys = [...selected];
    if (!keys.length) return;

    // Three-state on purpose: "leave as they are" is the difference between
    // editing five groups and accidentally painting them all one colour.
    const KEEP = { value: '', label: 'Leave as they are' };
    const answer = await window.shellmateDialog.form({
      title: `Edit ${keys.length} groups`,
      body:  'What you choose here applies to every selected group; '
             + '"leave as they are" keeps each group\'s own setting.',
      confirmLabel: 'Apply',
      fields: [
        { name: 'colour', label: 'Colour', type: 'select',
          disabled: !_coloursOn(),
          hint: _coloursOn() ? '' : 'Group colours are switched off in Settings.',
          options: [KEEP, ...colours.map(c => ({ value: c, label: _colourName(c) }))] },
        { name: 'favourite', label: 'Pinned to the top', type: 'select',
          options: [KEEP, { value: 'pin', label: 'Pinned' },
                          { value: 'unpin', label: 'Not pinned' }] },
      ],
    });
    if (!answer) return;

    const changes = {};
    if (answer.colour) changes.colour = answer.colour;
    if (answer.favourite) changes.favourite = answer.favourite === 'pin';
    if (!Object.keys(changes).length) return;

    const failed = [];
    for (const key of keys) {
      try {
        const res = await fetch('/api/groups/' + encodeURIComponent(key), {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(changes),
        });
        if (!res.ok) failed.push(key);
      } catch (_) {
        failed.push(key);
      }
    }
    if (failed.length) {
      _warn('Some groups were not updated', failed.join(', '));
    }
    _refresh();
  }

  /** Delete every selected group. One confirmation, named in full. */
  async function _bulkDelete() {
    const keys = [...selected];
    if (!keys.length) return;

    const names = keys.map(k => {
      const group = groupCache.find(g => g.key === k);
      return group ? group.name : k;
    });
    // Unique across the lot — a device in two selected groups is one device.
    const affected = new Set();
    keys.forEach(k => _membersUnder(k).forEach(p => affected.add(p.id)));

    const ok = await window.shellmateDialog.confirm({
      title: `Delete ${keys.length} groups?`,
      body:  (affected.size
        ? `The ${affected.size} connection${affected.size === 1 ? '' : 's'} in them `
          + 'are kept — they simply stop being grouped. '
        : '')
        + 'Each group goes along with any subgroups beneath it.',
      list:  names.map(text => ({ text })),
      confirmLabel: 'Delete them',
      danger: true,
    });
    if (!ok) return;

    // Longest key first, so a selected subgroup is deleted before the parent
    // whose deletion would take it anyway — no request ever 404s on a key
    // an earlier delete already swept up.
    keys.sort((a, b) => b.length - a.length);
    const failed = [];
    for (const key of keys) {
      try {
        const res = await fetch('/api/groups/' + encodeURIComponent(key),
                                { method: 'DELETE' });
        if (!res.ok) failed.push(key);
      } catch (_) {
        failed.push(key);
      }
    }
    _clearSelection();
    if (failed.length) {
      _warn('Some groups were not deleted', failed.join(', '));
    } else if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        title: `${keys.length} groups deleted`,
        body:  affected.size
          ? `${affected.size} connection${affected.size === 1 ? '' : 's'} kept.`
          : '',
      });
    }
    _refresh();
  }

  // -------------------------------------------------------------------------
  // Dragging
  // -------------------------------------------------------------------------

  let dragKey = '';

  function _bindDrag(tile, group) {
    tile.addEventListener('dragstart', (e) => {
      dragKey = group.key;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', group.key);
      tile.classList.add('group-dragging');
    });
    tile.addEventListener('dragend', () => {
      dragKey = '';
      tile.classList.remove('group-dragging');
      document.querySelectorAll('.group-drop').forEach(
        el => el.classList.remove('group-drop'));
    });

    tile.addEventListener('dragover', (e) => {
      e.preventDefault();
      tile.classList.add('group-drop');
    });
    tile.addEventListener('dragleave', () => tile.classList.remove('group-drop'));

    tile.addEventListener('drop', async (e) => {
      e.preventDefault();
      tile.classList.remove('group-drop');

      // A connection dropped on a group joins it. Adding, never moving — it
      // keeps every other group it was already in.
      const profileId = e.dataTransfer.getData('application/x-shellmate-profile');
      if (profileId) {
        await _addMember(group, profileId);
        return;
      }

      // Otherwise a group was dragged, and this is a rearrangement.
      if (!dragKey || dragKey === group.key) return;
      const keys = groupCache.map(g => g.key).filter(k => k !== dragKey);
      keys.splice(keys.indexOf(group.key), 0, dragKey);
      try {
        await fetch('/api/groups/order', {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ keys }),
        });
      } catch (_) { /* the order is a preference, not the data */ }
      _refresh();
    });
  }

  async function _addMember(group, profileId) {
    try {
      const res = await fetch(
        `/api/groups/${encodeURIComponent(group.key)}/members`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ profile_id: profileId, member: true }),
        });
      if (!res.ok) throw new Error('Could not add it.');
      window.dispatchEvent(new CustomEvent('shellmate:groups-changed'));
      _refresh();
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          title: `Added to "${group.name}"`,
          body:  'It keeps any other groups it was already in.',
        });
      }
    } catch (e) {
      _warn('Could not add it to the group', e.message);
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function _refresh() {
    // Before the redraw, not after. The event below used to be dispatched
    // last, so the render it was meant to invalidate had already run against
    // the cache it was meant to expire.
    groupsStale = true;
    liveStale = true;
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
    // The tab strip narrows to the selected group (#165) and has no other
    // way to hear that the selection changed.
    if (typeof window.repaintTabGroups === 'function') window.repaintTabGroups();
    // The tab strip carries group colours too (#140), and it has no other way
    // to know a colour changed.
    window.dispatchEvent(new CustomEvent('shellmate:groups-changed'));
  }

  function _colourName(key) {
    return key.charAt(0).toUpperCase() + key.slice(1);
  }

  function _warn(title, body) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error', title, body: body || '',
      });
    }
  }

  window.shellmateGroups = {
    render, active, activeName, activePath, activeIcon, showsTiles,
    open, clear, newGroup, editGroup, deleteGroup,
  };
})();
