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

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('btn-new-group');
    if (button) button.addEventListener('click', () => newGroup());
    _bindPanelControls();
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
  function _matches(node) {
    if (!query) return true;
    if (node.key.toLowerCase().includes(query)) return true;
    // The devices inside it too, so searching a hostname finds its group.
    return (_byTag.get(node.key) || []).some(p =>
      (p.name || '').toLowerCase().includes(query) ||
      (p.hostname || '').toLowerCase().includes(query));
  }

  /** True when this branch, or anything beneath it, matches. */
  function _branchMatches(node) {
    return _matches(node) || node.children.some(_branchMatches);
  }

  /** The tree node for a key, so a menu can ask about its children. */
  function _findNode(key) {
    let found = null;
    const walk = (nodes) => nodes.forEach(n => {
      if (n.key === key) found = n; else walk(n.children);
    });
    walk(_tree(groupCache));
    return found;
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

  function renderTree(profiles) {
    const panel = document.getElementById('group-tree');
    const body = document.getElementById('group-tree-body');
    if (!panel || !body) return;

    profileCache = profiles || [];
    _indexProfiles();

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
    let roots = _tree(groupCache);
    if (query) {
      // Only branches with a match somewhere beneath them, each one forced
      // open so the match is actually reachable.
      roots = roots.filter(_branchMatches);
      matchOpen = new Set();
      const mark = (n) => {
        if (n.children.some(_branchMatches)) matchOpen.add(n.key);
        n.children.forEach(mark);
      };
      roots.forEach(mark);
    }
    const pinned = roots.filter(n => n.group && n.group.favourite);
    const rest = roots.filter(n => !(n.group && n.group.favourite));

    pinned.forEach(node => body.appendChild(_branch(node)));
    if (pinned.length && rest.length) {
      const rule = document.createElement('div');
      rule.className = 'tree-pin-divider';
      body.appendChild(rule);
    }
    rest.forEach(node => body.appendChild(_branch(node)));

    // "Everything", so there is always a way back to the whole list without
    // hunting for which group is currently selected.
    const all = document.createElement('button');
    all.type = 'button';
    all.className = 'tree-all' + (activeGroup ? '' : ' tree-active');
    all.textContent = `All connections (${profileCache.length})`;
    all.addEventListener('click', () => { activeGroup = ''; _select(); });
    body.insertBefore(all, body.firstChild);
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
                   + (node.group && node.group.favourite ? ' group-favourite' : '');
    chip.dataset.key = node.key;

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
    chip.addEventListener('click', () => {
      // Clicking opens and closes it (#200), and selects it.
      //
      // It used to toggle the *selection* and only ever expand, so a second
      // click on the same group deselected it while leaving it open — which
      // read as the click doing nothing. Selection cannot simply move to the
      // twist arrow: it decides which tiles are painted (#177) and what the
      // hero names (#179), so a click does both and "All connections" stays
      // the way to clear it. Collapsing and emptying the dashboard on one
      // click would be two answers to one gesture.
      if (expanded.has(node.key)) expanded.delete(node.key);
      else expanded.add(node.key);
      activeGroup = node.key;
      _select();
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
    if (node.group) {
      (_byTag.get(node.key) || [])
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
    menu.style.left = event.clientX + 'px';
    menu.style.top = event.clientY + 'px';
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
    chip.addEventListener('dragover', (e) => {
      e.preventDefault();
      chip.classList.add('group-drop');
    });
    chip.addEventListener('dragleave', () => chip.classList.remove('group-drop'));
    chip.addEventListener('drop', async (e) => {
      e.preventDefault();
      chip.classList.remove('group-drop');
      const profileId = e.dataTransfer.getData('application/x-shellmate-profile');
      if (profileId && node.group) await _addMember(node.group, profileId);
    });
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
    const count = (_byTag.get(group.key) || []).length;
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
    const members = _byTag.get(group.key) || [];
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

  async function newGroup() {
    const answer = await window.shellmateDialog.form({
      title: 'New group',
      body:  'Groups arrange the dashboard. A connection can be in as many as '
             + 'you like — adding it to one never takes it out of another.',
      confirmLabel: 'Create',
      fields: [
        { name: 'name', label: 'Name', required: true, placeholder: 'Glasgow',
          hint: 'If you already tag connections with this name, they join it.' },
        { name: 'colour', label: 'Colour', type: 'select',
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
      ],
    });
    if (!answer) return;

    try {
      const res = await fetch('/api/groups', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(answer),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not create it.');
      const group = await res.json();
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
    const answer = await window.shellmateDialog.form({
      title: 'New group inside "' + parent.name + '"',
      body:  'It appears as a branch under the parent, and the parent counts '
             + 'everything beneath it.',
      confirmLabel: 'Create',
      fields: [
        { name: 'name', label: 'Name', required: true, placeholder: 'access',
          hint: 'Stored as "' + parent.name + '/...", which is what makes it nest.' },
        { name: 'colour', label: 'Colour', type: 'select',
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
      ],
    });
    if (!answer) return;

    try {
      const res = await fetch('/api/groups', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ name: parent.name + '/' + answer.name,
                                  colour: answer.colour }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not create it.');
      const group = await res.json();
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
    const current = group.icon || 'folder';

    const grid = document.createElement('div');
    grid.className = 'icon-picker';
    let chosen = current;

    icons.forEach(name => {
      const cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'icon-picker-cell' + (name === current ? ' chosen' : '');
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

    const ok = await window.shellmateDialog.confirm({
      title: 'Icon for "' + group.name + '"',
      body:  'It appears beside the name in the tree and on the tab strip.',
      confirmLabel: 'Use it',
      content: grid,
    });
    if (!ok) return;

    try {
      const res = await fetch('/api/groups/' + encodeURIComponent(group.key), {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ icon: chosen }),
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
    const ok = await window.shellmateDialog.confirm({
      title: `Delete the group "${group.name}"?`,
      body:  group.count
        ? `The ${group.count} connection${group.count === 1 ? '' : 's'} in it are kept — `
          + 'they simply stop being grouped. Only the group and its colour go.'
        : 'It is empty, so nothing else changes.',
      confirmLabel: 'Delete the group',
      danger: true,
    });
    if (!ok) return;

    try {
      const res = await fetch(`/api/groups/${encodeURIComponent(group.key)}`,
                              { method: 'DELETE' });
      if (!res.ok) throw new Error('Could not delete it.');
      const data = await res.json();
      if (activeGroup === group.key) activeGroup = '';
      _refresh();
      if (window.shellmateAlerts && data.released) {
        window.shellmateAlerts.notify({
          title: `Group "${group.name}" deleted`,
          body:  `${data.released} connection${data.released === 1 ? '' : 's'} kept.`,
        });
      }
    } catch (e) {
      _warn('Could not delete the group', e.message);
    }
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
    document.querySelectorAll('.group-menu').forEach(el => el.remove());

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu group-menu';

    const item = (icon, text, onClick, danger) => {
      const button = document.createElement('button');
      button.type = 'button';
      if (danger) button.className = 'ctx-danger';
      button.innerHTML = `<span class="material-symbols-outlined">${icon}</span>`;
      button.appendChild(document.createTextNode(text));
      button.addEventListener('click', () => { menu.remove(); onClick(); });
      return button;
    };

    menu.appendChild(item('tune', 'Rename or recolour…', () => editGroup(group)));
    menu.appendChild(item('palette', 'Change the icon...', () => pickIcon(group)));
    // Nesting was already rendered - a group named "site-3/access" becomes
    // a branch under "site-3" - but the only way to make one was typing the
    // separator by hand into the name field, which nothing explained (#163).
    menu.appendChild(item('add', 'New subgroup...', () => newSubgroup(group)));

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
    menu.appendChild(item('add_circle', 'Connect all',
                          () => _connectAll(group)));
    menu.appendChild(item('stop_circle', 'Disconnect all',
                          () => _disconnectAll(group), true));
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
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;

    setTimeout(() => {
      document.addEventListener('click', () => menu.remove(), { once: true });
    }, 0);
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
    open, newGroup, editGroup, deleteGroup,
  };
})();
