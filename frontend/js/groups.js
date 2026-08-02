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

  /** Cached so a re-render does not wait on a request. */
  let groupCache = [];
  let colours = ['slate'];

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('btn-new-group');
    if (button) button.addEventListener('click', () => newGroup());
    _bindPanelControls();
  });

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  // Recount the live badges when a session opens or closes.
  window.addEventListener('shellmate:sessions-changed', () => {
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
  });

  function active() { return activeGroup; }

  function activeName() {
    const group = groupCache.find(g => g.key === activeGroup);
    return group ? group.name : activeGroup;
  }

  function open(key) {
    activeGroup = (activeGroup === key) ? '' : key;
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
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
  async function render(profiles) {
    try {
      const res = await fetch('/api/groups');
      const data = res.ok ? await res.json() : { groups: [] };
      groupCache = data.groups || [];
      if (data.colours && data.colours.length) colours = data.colours;
    } catch (_) {
      groupCache = [];
    }

    // A group that was open and has since been deleted must not leave the
    // dashboard filtered to nothing with no way back.
    if (activeGroup && !groupCache.some(g => g.key === activeGroup)) {
      activeGroup = '';
    }

    await _loadLive();
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

  /** Session keys ("address:port") that are open right now. */
  let liveKeys = new Set();

  async function _loadLive() {
    try {
      const res = await fetch('/api/sessions');
      const list = res.ok ? await res.json() : [];
      liveKeys = new Set(list
        .filter(s => s.is_connected)
        .map(s => `${(s.address || s.hostname || '').toLowerCase()}:${s.port || 0}`));
    } catch (_) {
      liveKeys = new Set();
    }
  }

  /**
   * How many connections in this branch are open.
   *
   * Matched on address and port exactly, not loosely. #124 is the cautionary
   * tale: matching a session to a profile approximately is how restore
   * connected to the wrong device. Counting is harmless where connecting is
   * not, but the two should still agree about what "this device" means.
   */
  function _liveUnder(node) {
    let total = 0;
    if (node.group) {
      total += profileCache.filter(p =>
        (p.tags || []).includes(node.key) &&
        liveKeys.has(`${(p.hostname || '').toLowerCase()}:${p.port || 0}`)).length;
    }
    node.children.forEach(child => { total += _liveUnder(child); });
    return total;
  }

  /** Everything in this branch, including nested groups. */
  function _countUnder(node) {
    let total = node.group ? node.group.count : 0;
    node.children.forEach(child => { total += _countUnder(child); });
    return total;
  }

  function renderTree(profiles) {
    const panel = document.getElementById('group-tree');
    const body = document.getElementById('group-tree-body');
    if (!panel || !body) return;

    profileCache = profiles || [];

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
    _tree(groupCache).forEach(node => body.appendChild(_branch(node)));

    // "Everything", so there is always a way back to the whole list without
    // hunting for which group is currently selected.
    const all = document.createElement('button');
    all.type = 'button';
    all.className = 'tree-all' + (activeGroup ? '' : ' tree-active');
    all.textContent = `All connections (${profileCache.length})`;
    all.addEventListener('click', () => { activeGroup = ''; _refresh(); });
    body.insertBefore(all, body.firstChild);
  }

  /** One branch: the group chip, its children, and its connections. */
  function _branch(node) {
    const wrap = document.createElement('div');
    wrap.className = 'tree-branch';
    wrap.style.setProperty('--depth', String(node.depth));

    const row = document.createElement('div');
    row.className = 'tree-row';

    const hasChildren = node.children.length > 0;
    const open = expanded.has(node.key);

    const twist = document.createElement('button');
    twist.type = 'button';
    twist.className = 'tree-twist' + (hasChildren || node.group ? '' : ' tree-twist-empty');
    twist.innerHTML = '<span class="material-symbols-outlined">'
                    + (open ? 'keyboard_arrow_down' : 'keyboard_arrow_up')
                    + '</span>';
    twist.addEventListener('click', (e) => {
      e.stopPropagation();
      if (open) expanded.delete(node.key); else expanded.add(node.key);
      _refresh();
    });

    // The chip, kept from the tiles — that part was right.
    const chip = document.createElement('button');
    chip.type = 'button';
    const colour = node.group ? node.group.colour : 'slate';
    chip.className = `tree-chip group-${colour}`
                   + (activeGroup === node.key ? ' tree-active' : '')
                   + (node.group && node.group.favourite ? ' group-favourite' : '');
    chip.dataset.key = node.key;

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
    if (live) {
      const badge = document.createElement('span');
      badge.className = 'tree-chip-live';
      badge.textContent = live;
      badge.title = `${live} open now`;
      chip.appendChild(badge);
    }

    chip.append(name, count);
    chip.addEventListener('click', () => {
      // Selecting filters the grid; it no longer replaces the view, so the
      // rest of the tree stays visible beside what it filtered to.
      activeGroup = (activeGroup === node.key) ? '' : node.key;
      expanded.add(node.key);
      _refresh();
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

    node.children.forEach(child => wrap.appendChild(_branch(child)));

    // The connections themselves, which is the half a tile could never show.
    if (node.group) {
      profileCache
        .filter(p => (p.tags || []).includes(node.key))
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

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = 'terminal';

    const label = document.createElement('span');
    label.className = 'tree-leaf-name';
    label.textContent = profile.name || profile.hostname || '';

    leaf.append(icon, label);
    leaf.addEventListener('click', () => {
      if (typeof window.connectProfile === 'function') window.connectProfile(profile);
      else if (typeof window.showConnectionDialog === 'function') {
        window.showConnectionDialog(profile);
      }
    });
    return leaf;
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
      }
    });

    _bindResize();

    const dock = document.getElementById('group-tree-dock');
    if (dock) dock.addEventListener('click', (e) => {
      e.stopPropagation();
      const side = _prefs().group_tree_side === 'right' ? 'left' : 'right';
      if (window.shellmatePrefs) window.shellmatePrefs.set('group_tree_side', side);
      renderTree(profileCache);
    });

    const collapse = document.getElementById('group-tree-collapse');
    if (collapse) collapse.addEventListener('click', (e) => {
      e.stopPropagation();
      const now = !(_prefs().group_tree_collapsed === true);
      if (window.shellmatePrefs) window.shellmatePrefs.set('group_tree_collapsed', now);
      renderTree(profileCache);
    });
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

  async function editGroup(group) {
    const answer = await window.shellmateDialog.form({
      title: `Edit "${group.name}"`,
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Name', value: group.name, required: true,
          hint: 'Renaming re-tags every connection in the group.' },
        { name: 'colour', label: 'Colour', type: 'select', value: group.colour,
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
        { name: 'favourite', label: 'Favourite', type: 'checkbox',
          value: group.favourite, hint: 'Favourites sit at the front.' },
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
    menu.appendChild(item(
      'bookmark_add',
      group.favourite ? 'Remove from favourites' : 'Add to favourites',
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
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
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
    render, active, activeName, open, newGroup, editGroup, deleteGroup,
  };
})();
