"""
groups.py — Groups on the dashboard.

A group is a **tag with a face**. Membership is the tag itself — the thing
already stored on every connection, already normalised, already searchable and
already used to group the tab strip — and this module adds only what a tag
cannot express: a display name, a colour, whether it is a favourite, and where
it sits on the dashboard.

That choice is deliberate and it is why there is no migration and no second
taxonomy. The alternative — exclusive folders with a ``group_id`` per profile —
would leave tags and groups as two overlapping ways to say the same thing,
which is the outcome worth avoiding. It also matches what the dashboard markup
already argued for:

    Tags rather than folders, because the useful ones overlap: a device is
    both "glasgow" and "production" and "access".

**Every tag is a group.** A tag in use with no entry here is still shown, with
default presentation. An entry here with no members is still shown, which is
the one thing a tag alone cannot do — you have to be able to make an empty
group before you can put anything in it.

Stored in ``groups.json`` beside the other data files rather than inside
``profiles.json``, which is a bare JSON array and stays one.
"""

import functools
import json
import logging
import re
import threading
import uuid
from typing import Any

from backend import jsonfile, paths, profiles as profiles_module

# Every change to a data file is a load → change → save cycle, and two of
# them at once lose an edit or the whole file (#457). One re-entrant lock
# per module around each public mutator; jsonfile adds the atomic write.
_lock = threading.RLock()


def _synchronised(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return inner

logger = logging.getLogger(__name__)

# Anything that is not a letter or a digit separates words, so "site-004",
# "out_of_band" and "core switches" all split the same way.
_WORD_RE = re.compile(r"[^a-z0-9]+")

# Colours are named, not hex.
#
# A raw hex value chosen against the dark theme is unreadable in the light one,
# which is the exact bug test_contrast.py exists to prevent. These map to
# tokens the stylesheet defines twice, so a group looks deliberate in both.
COLOURS = (
    "slate", "blue", "green", "amber", "red", "purple", "teal", "pink",
)
DEFAULT_COLOUR = "slate"

# Icons are a fixed list, and it has to stay one.
#
# The Material Symbols font is subsetted at build time from the names the
# source references, and a glyph outside the subset renders as its own name in
# plain text with no error anywhere. So a free-text icon field would let
# somebody store "gavel" and ship a group labelled the word "gavel". Every
# name here is in the source, so every name here is in the font, and
# test_icons.py holds that true.
ICONS = (
    "folder", "apartment", "location_city", "lan", "router", "security",
    "wifi", "public", "cable", "hub", "call", "settings", "dns", "storage",
    "cloud", "science", "print", "power", "lock", "memory", "monitor",
    "videocam", "sensors", "home",
)
DEFAULT_ICON = "folder"

# Which words mean which icon, most specific first.
#
# Matched on **whole words**, not substrings. "core switches" and "switching"
# both mean a switch, but a substring match would read "site-004" as "it" and
# "management" as "man" — and the estate is nothing but names like site-004.
ICON_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("firewall", "firewalls", "asa", "palo", "fortigate", "security"), "security"),
    (("switch", "switches", "switching", "access", "dist", "distribution",
      "core"), "lan"),
    (("router", "routers", "wan", "edge", "mpls"), "router"),
    (("wireless", "wifi", "wlan", "ap", "aps"), "wifi"),
    (("internet", "isp", "external", "public", "dmz"), "public"),
    (("oob", "console", "serial", "out"), "cable"),
    (("lb", "balancer", "balancers", "f5", "loadbalancer"), "hub"),
    (("voice", "voip", "telephony", "pbx", "gateway", "gateways"), "call"),
    (("mgmt", "management", "admin", "oam"), "settings"),
    (("server", "servers", "dns", "dhcp", "ntp"), "dns"),
    (("datacentre", "datacenter", "dc", "rack", "racks", "storage", "san"),
     "storage"),
    (("cloud", "aws", "azure", "gcp"), "cloud"),
    (("lab", "test", "staging", "dev", "sandbox"), "science"),
    (("printer", "printers", "print"), "print"),
    (("ups", "pdu", "power"), "power"),
    (("camera", "cameras", "cctv", "nvr"), "videocam"),
    (("sensor", "sensors", "iot"), "sensors"),
    (("home", "house"), "home"),
    (("site", "sites", "campus", "branch", "office", "building"), "apartment"),
    (("city", "region", "country", "estate"), "location_city"),
)


def guess_icon(name: str) -> str:
    """
    An icon for a group, from the words in its name (#180).

    Only the last path segment: `site-004/firewalls` is a firewall group, and
    matching the whole key would let the site's own icon win over it. Falls
    back to a folder, which is honest about knowing nothing rather than
    picking something confidently wrong.
    """
    segment = (name or "").split("/")[-1].lower()
    words = {w for w in _WORD_RE.split(segment) if w}
    for candidates, icon in ICON_WORDS:
        if words.intersection(candidates):
            return icon
    return DEFAULT_ICON


# ---------------------------------------------------------------------------
# Defaults a group lends its connections (#545)
#
# Two hundred sites, each behind its own bastion with its own TACACS realm,
# is 200 x N connections to edit the day a bastion moves. A group is the one
# place that fact can be stated once, so it is stated here and every
# connection in the group borrows it.
#
# **Borrowed, never written.** A default fills a field the connection leaves
# blank; nothing is copied onto the profile, so changing the group changes
# every device relying on it. A field the connection sets itself always wins
# - somebody who typed a jump host on one switch meant it for that switch.
#
# And **ambiguity inherits nothing**. A device in `glasgow` and in `core`
# where the two disagree about the jump host has no answer that is not a
# guess, so it gets none and the dialog says which groups disagree. Guessing
# here means dialling the wrong bastion with the wrong credential, which is
# how a tool ends up authenticating against somebody else's realm.
# ---------------------------------------------------------------------------

#: What a group may lend. Deliberately none of the identifying fields: a
#: group cannot lend a hostname or a COM port, because those are what make a
#: connection that connection.
DEFAULT_FIELDS = (
    "username",
    "port",
    "platform",
    "credential_ref",
    "jump_host",
    "jump_port",
    "jump_username",
)

#: The ones that are numbers, and have to be in range.
_PORT_FIELDS = ("port", "jump_port")


def _clean_defaults(values) -> dict:
    """
    One group's defaults as stored: known fields only, ports in range.

    Raises:
        ValueError: A secret was passed. Refused rather than stripped, for
            the reason profiles.SECRET_FIELDS exists - groups.json is plain
            JSON on disk, and a caller that believes it stored a password
            there is worse off than one told it cannot.
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("Group defaults have to be a set of fields.")

    offered = set(values) & profiles_module.SECRET_FIELDS
    if offered:
        raise ValueError(
            "A group cannot hold a password. Make a shared credential and "
            "give the group that instead."
        )

    out: dict = {}
    for field in DEFAULT_FIELDS:
        raw = values.get(field)
        if raw is None or raw == "":
            continue
        if field in _PORT_FIELDS:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"'{raw}' is not a port number.") from None
            if not 1 <= number <= 65535:
                raise ValueError(f"{number} is not a port number.")
            out[field] = number
        else:
            out[field] = " ".join(str(raw).split())
    return out


# The defaults of every group, kept until groups.json changes. Listing five
# thousand connections asks for them once per connection, and re-reading the
# file each time is the shape of mistake profiles._cache exists to record.
_defaults_cache: dict = {"key": None, "table": None}


def default_table() -> dict[str, dict]:
    """Every group that lends something, keyed by group key."""
    path = _file()
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None

    with _lock:
        if _defaults_cache["key"] != stamp or _defaults_cache["table"] is None:
            _defaults_cache["key"] = stamp
            _defaults_cache["table"] = {
                entry["key"]: entry["defaults"]
                for entry in _load()
                if entry.get("key") and isinstance(entry.get("defaults"), dict)
                and entry["defaults"]
            }
        # A copy: callers read this beside a profile they are about to
        # change, and a shared dict would let one of them edit the store.
        return {key: dict(value)
                for key, value in _defaults_cache["table"].items()}


def _ancestry(tag: str) -> list[str]:
    """A tag and the groups above it, nearest first: a/b/c, a/b, a."""
    parts = [part for part in (tag or "").split("/") if part]
    return ["/".join(parts[:depth]) for depth in range(len(parts), 0, -1)]


def defaults_for(tags, table: dict | None = None) -> dict:
    """
    What a connection in these groups inherits, and from where.

    The nearest ancestor wins within one branch - `site-004/access` before
    `site-004` - because the subgroup is the more specific statement. Across
    *different* branches there is no nearer and no further, so two groups
    that disagree about a field cancel it out entirely and the field is
    reported in ``conflicts`` instead. Two that happen to agree are not a
    conflict: there is still only one answer.

    Returns:
        ``{"values": {field: value}, "from": {field: group_key},
        "conflicts": {field: [group_key, ...]}}``
    """
    table = default_table() if table is None else table
    if not table:
        return {"values": {}, "from": {}, "conflicts": {}}

    values: dict = {}
    sources: dict = {}
    conflicts: dict = {}

    for tag in tags or []:
        # One branch resolved first, so the nearer group wins before anything
        # is compared against another branch.
        claimed: dict = {}
        for key in _ancestry(tag):
            for field, value in (table.get(key) or {}).items():
                claimed.setdefault(field, (value, key))

        for field, (value, key) in claimed.items():
            if field in conflicts:
                if key not in conflicts[field]:
                    conflicts[field].append(key)
            elif field not in values:
                values[field] = value
                sources[field] = key
            elif values[field] != value:
                conflicts[field] = [sources.pop(field), key]
                values.pop(field)

    return {"values": values, "from": sources, "conflicts": conflicts}


def _file():
    return paths.data_dir() / "groups.json"


def _load() -> list[dict]:
    path = _file()
    if not path.exists():
        return []
    # A corrupt groups file must not take the dashboard down with it.
    # Presentation is the only thing lost; every group still exists, because
    # the membership lives on the connections. jsonfile sets the bad file
    # aside rather than letting the next save overwrite it.
    return jsonfile.read(path, [], expect=list)


def _save(groups: list[dict]) -> None:
    jsonfile.write(_file(), groups)


def _key(name: str) -> str:
    """The tag a group name corresponds to."""
    cleaned = profiles_module.normalise_tags([name])
    return cleaned[0] if cleaned else ""


def list_groups() -> list[dict]:
    """
    Every group: those given presentation here, plus every tag in use.

    Merged rather than concatenated, so a tag that later gains a colour does
    not appear twice — the two sources are two halves of one group.
    """
    stored = {g["key"]: g for g in _load() if g.get("key")}
    counts = {t["tag"]: t["count"] for t in profiles_module.all_tags()}

    out: list[dict] = []
    for key in sorted(set(stored) | set(counts)):
        entry = stored.get(key, {})
        out.append({
            "key":       key,
            # The display name keeps the capitalisation somebody typed; the
            # key is lower-cased because "Production" and "production" being
            # two groups is a distinction nobody means to make.
            "name":      entry.get("name") or key,
            "colour":    entry.get("colour") if entry.get("colour") in COLOURS
                         else DEFAULT_COLOUR,
            # Guessed rather than stored where nobody has chosen one, so an
            # implicit group — and every group that predates icons — gets a
            # sensible face without a migration. An unknown stored value falls
            # back the same way a bad colour does; it must never render
            # nothing.
            "icon":      entry.get("icon") if entry.get("icon") in ICONS
                         else guess_icon(entry.get("name") or key),
            "favourite": bool(entry.get("favourite")),
            "order":     entry.get("order", 1000),
            "count":     counts.get(key, 0),
            # True when it exists only because something is tagged with it —
            # the interface can offer to give it a colour rather than treating
            # it as already configured.
            "implicit":  key not in stored,
            # What this group lends its connections (#545). Always a dict,
            # so the editor has something to render either way.
            "defaults":    entry.get("defaults") if isinstance(entry.get("defaults"), dict) else {},
            # Scheduled backups (#408): the schedule, and how the last one went.
            "backup":      entry.get("backup") or None,
            "backup_last": entry.get("backup_last") or None,
            # Compliance (#543): the standing choice of what this group is
            # checked against, and the last result. Projected here as well
            # as accepted in update_group — this function builds the public
            # view field by field, so a key stored and not named here is a
            # key nothing can read back.
            "compliance":      entry.get("compliance") or None,
            "compliance_last": entry.get("compliance_last") or None,
        })

    # Favourites first, then the hand-arranged order, then alphabetically for
    # anything never dragged.
    out.sort(key=lambda g: (not g["favourite"], g["order"], g["name"].lower()))
    return out


def get_group(key: str) -> dict | None:
    key = _key(key)
    return next((g for g in list_groups() if g["key"] == key), None)


@_synchronised
def create_group(name: str, colour: str = DEFAULT_COLOUR,
                 icon: str = "") -> dict:
    """
    Create a group, or adopt an existing tag as one.

    Adoption rather than refusal: somebody who has been tagging connections
    "glasgow" for a month and then makes a Glasgow group means the same thing
    by it, and creating a second empty one beside it would be absurd.
    """
    key = _key(name)
    if not key:
        raise ValueError("A group needs a name.")

    groups = _load()
    existing = next((g for g in groups if g.get("key") == key), None)
    if existing:
        existing["name"] = name.strip() or existing.get("name") or key
        if colour in COLOURS:
            existing["colour"] = colour
        if icon in ICONS:
            existing["icon"] = icon
        _save(groups)
    else:
        groups.append({
            "id":        str(uuid.uuid4()),
            "key":       key,
            "name":      name.strip(),
            "colour":    colour if colour in COLOURS else DEFAULT_COLOUR,
            # Guessed at creation, so a group has a face without anybody
            # being asked for one. Stored, not re-derived, so a later rename
            # does not silently change an icon somebody had settled on.
            "icon":      icon if icon in ICONS else guess_icon(name),
            "favourite": False,
            # New groups go to the front, where the person who just made one
            # is looking.
            "order":     min((g.get("order", 1000) for g in groups), default=1000) - 1,
        })
        _save(groups)

    return get_group(key)


@_synchronised
def update_group(key: str, changes: dict) -> dict:
    """
    Rename, recolour, favourite or reposition a group.

    A rename is the interesting one: the name *is* the tag, so it has to be
    rewritten on every connection carrying it. Done here rather than left to
    the caller, because a half-applied rename splits a group in two.
    """
    key = _key(key)
    groups = _load()
    entry = next((g for g in groups if g.get("key") == key), None)

    if entry is None:
        # An implicit group — a tag nobody has styled yet. Give it an entry so
        # there is somewhere to record the change.
        entry = {"id": str(uuid.uuid4()), "key": key, "name": key,
                 "colour": DEFAULT_COLOUR, "favourite": False, "order": 1000}
        groups.append(entry)

    if "colour" in changes and changes["colour"] in COLOURS:
        entry["colour"] = changes["colour"]
    if "icon" in changes and changes["icon"] in ICONS:
        entry["icon"] = changes["icon"]
    if "favourite" in changes:
        entry["favourite"] = bool(changes["favourite"])
    if "backup" in changes:
        from backend import scheduler
        plan = changes["backup"]
        if plan and isinstance(plan, dict):
            kept = scheduler.normalise(plan) or {"enabled": False}
            if plan.get("armed_at"):
                kept["armed_at"] = plan["armed_at"]
            entry["backup"] = kept
        else:
            entry.pop("backup", None)
    if "defaults" in changes:
        # Cleaned rather than trusted, and the empty case removes the key
        # rather than storing {} - a group that lends nothing should read
        # as one in the file too. _clean_defaults raises on a secret.
        cleaned = _clean_defaults(changes["defaults"])
        if cleaned:
            entry["defaults"] = cleaned
        else:
            entry.pop("defaults", None)
    if "backup_last" in changes and isinstance(changes["backup_last"], dict):
        entry["backup_last"] = changes["backup_last"]
    # Compliance (#543): the standing choice of what to check against, and
    # the last result. Both have to be named here — this function handles
    # the keys it knows and silently drops the rest, which is how a field
    # can be written, returned as accepted, and never persisted.
    if "compliance" in changes and isinstance(changes["compliance"], dict):
        entry["compliance"] = changes["compliance"]
    if "compliance_last" in changes and isinstance(changes["compliance_last"], dict):
        entry["compliance_last"] = changes["compliance_last"]
    if "order" in changes:
        try:
            entry["order"] = int(changes["order"])
        except (TypeError, ValueError):
            pass

    new_name = (changes.get("name") or "").strip()
    if new_name:
        new_key = _key(new_name)
        if not new_key:
            raise ValueError("A group needs a name.")
        if new_key != key:
            if any(g.get("key") == new_key for g in groups if g is not entry):
                raise ValueError(f"A group called '{new_name}' already exists.")

            # The subtree moves with it (#294) — the *whole* subtree (#321).
            #
            # Nesting is the name, so `glasgow` becoming `edinburgh` leaves
            # `glasgow/switches` naming a parent that no longer exists — the
            # branch and every connection in it orphaned, present in the file
            # and absent from the tree. And "every tag is a group": a nested
            # tag nobody ever styled is a subgroup too, so the sweep covers
            # tags in use as well as entries in this file, exactly as
            # delete_group does.
            prefix = f"{key}/"
            moving = {key}
            for child in groups:
                child_key = child.get("key") or ""
                if child_key.startswith(prefix):
                    moving.add(child_key)
            for tag in profiles_module.all_tags():
                if tag["tag"].startswith(prefix):
                    moving.add(tag["tag"])

            renames = {old: new_key + old[len(key):] for old in moving}
            for moved_to in renames.values():
                if any((g.get("key") or "") == moved_to
                       and (g.get("key") or "") not in moving
                       for g in groups):
                    raise ValueError(
                        f"Moving this would collide with '{moved_to}'."
                    )

            # Display names move by whole segments (#321). Slicing by the
            # length of the parent's *key* corrupted any child whose display
            # name normalises to a different length — "Glasgow  Site" has
            # thirteen characters, its key "glasgow site" twelve, and the
            # one-off slice mangled every child name under it.
            depth = key.count("/") + 1
            for child in groups:
                child_key = child.get("key") or ""
                if not child_key.startswith(prefix):
                    continue
                child["key"] = renames[child_key]
                tail = (child.get("name") or child_key).split("/")[depth:]
                child["name"] = "/".join([new_name, *tail])

            # One pass over the profiles for the whole subtree (#327), not a
            # file rewrite per connection per moved tag.
            profiles_module.retag_many(renames)
            entry["key"] = new_key
        entry["name"] = new_name

    _save(groups)
    return get_group(entry["key"])


@_synchronised
def clone_group(key: str, destination: str = "", name: str = "",
                include_connections: bool = False) -> dict:
    """
    Copy a group and everything nested under it, somewhere else (#598).

    Sites resemble each other — that is the whole reason the tree nests —
    so building the fifth one by retyping ten subgroups, their colours and
    their order is work the shape of the data says nobody should have to do.

    ``destination`` is the group to nest the copy under, or empty for the
    top level. ``name`` renames the copy; without it the original's name is
    reused, which is only legal when the destination differs.

    **Structure by default.** ``include_connections`` is off because the
    common case is "build site-5 like site-4", and forty connections all
    pointing at site-4's addresses is not a head start — it is forty wrong
    devices with real names, which is worse than none. Turning it on tags
    the existing connections into the new groups as well; it copies no
    connection records, because a second profile for one device is a
    different and worse mistake.

    Refuses rather than guesses:

    - cloning into itself or into its own descendant, which would recurse;
    - a destination that does not exist;
    - a name already taken at the destination, because group identity *is*
      the path and adopting a live group would silently merge two trees.
    """
    key = _key(key)
    source = get_group(key)
    if source is None and not any(
            g.get("key", "").startswith(f"{key}/") for g in _load()):
        raise ValueError("There is no such group.")

    leaf = key.rsplit("/", 1)[-1]
    new_leaf = _key(name) if name else leaf
    if not new_leaf:
        raise ValueError("The copy needs a name.")
    if "/" in new_leaf:
        raise ValueError("A name cannot contain '/': pick the destination "
                         "instead of typing a path.")

    destination = _key(destination) if destination else ""
    if destination:
        known = {g.get("key") for g in _load()}
        implied = {t for g in _load() for t in [g.get("key", "")] if t}
        if destination not in known and destination not in implied:
            raise ValueError("There is no such destination group.")

    target = f"{destination}/{new_leaf}" if destination else new_leaf

    # Into itself, or into its own subtree. The check is on the target
    # rather than the destination so that renaming out of the way is still
    # allowed: site-4 → site-4/archive is a loop, site-4 → site-5 is not.
    if target == key or target.startswith(f"{key}/"):
        raise ValueError("A group cannot be cloned into itself or into one "
                         "of its own subgroups.")

    groups = _load()
    existing = {g.get("key") for g in groups}
    if target in existing:
        raise ValueError(f"'{target}' already exists. Give the copy another "
                         "name, or choose a different destination.")

    # The subtree, stored or implied. A tag in use under the prefix is a
    # subgroup whether or not anybody gave it a colour, and leaving those
    # out would clone a structure with holes in it.
    prefix = f"{key}/"
    subtree = [g for g in groups
               if g.get("key") == key or g.get("key", "").startswith(prefix)]
    if not subtree:
        raise ValueError("There is no such group.")

    lowest = min((g.get("order", 1000) for g in groups), default=1000)
    made: list[str] = []
    for index, original in enumerate(sorted(
            subtree, key=lambda g: g.get("key", ""))):
        old_key = original.get("key", "")
        tail = old_key[len(key):].lstrip("/")
        fresh_key = f"{target}/{tail}" if tail else target
        if fresh_key in existing:
            continue
        display = original.get("name") or old_key
        # The leaf's own label is what a rename should change; the
        # descendants keep theirs, because "core switches" means the same
        # thing under any site.
        if not tail:
            display = name.strip() or display.rsplit("/", 1)[-1]
        groups.append({
            "id":        str(uuid.uuid4()),
            "key":       fresh_key,
            "name":      display,
            "colour":    original.get("colour", DEFAULT_COLOUR),
            "icon":      original.get("icon", ""),
            "favourite": bool(original.get("favourite")),
            "order":     lowest - len(subtree) + index,
            **({"defaults": dict(original["defaults"])}
               if original.get("defaults") else {}),
        })
        existing.add(fresh_key)
        made.append(fresh_key)
    _save(groups)

    tagged = 0
    if include_connections:
        tagged = _tag_into_clone(key, target)

    logger.info("Cloned %s to %s (%d group(s), %d connection(s) tagged)",
                key, target, len(made), tagged)
    return {"key": target, "created": made, "groups": len(made),
            "connections": tagged}


def _tag_into_clone(source: str, target: str) -> int:
    """
    Add the clone's tags to the connections already in the source subtree.

    Deliberately tagging rather than copying. A second profile for one
    device is two places to change its password and two rows claiming to be
    the same switch; a connection in two groups is exactly what the tag
    model is for, and it is what the estate already does everywhere else.
    """
    prefix = f"{source}/"
    touched = 0
    # The raw list, not get_profiles(): that decorates every connection with
    # its credential state, which is five thousand lookups to read a tag.
    for profile in profiles_module._load():
        tags = profiles_module.normalise_tags(profile.get("tags"))
        additions = []
        for tag in tags:
            tag_key = _key(tag)
            if tag_key == source or tag_key.startswith(prefix):
                tail = tag_key[len(source):].lstrip("/")
                additions.append(f"{target}/{tail}" if tail else target)
        fresh = [t for t in additions if t not in tags]
        if not fresh:
            continue
        profiles_module.set_tags(profile["id"], tags + fresh)
        touched += 1
    return touched


@_synchronised
def delete_group(key: str, delete_connections: bool = False) -> dict:
    """
    Remove a group and everything nested beneath it. **By default the
    connections in it survive.**

    Nothing else on the dashboard destroys anything, so this removes the tags
    and the presentation and leaves every connection exactly where it was.
    The count is returned so the interface can say what it let go of rather
    than what it deleted.

    The subtree goes too. Nesting is the name, so deleting ``site`` while
    ``site/access`` survives leaves the tree rebuilding ``site`` as a bare
    path segment — a folder-iconed ghost of the group that was just deleted,
    which reads as the deletion having failed.

    ``delete_connections`` is the other reading of "delete the group" (#360):
    the connections go with it. Kept as an explicit choice rather than the
    default because it takes saved credentials with it and cannot be undone.
    It deletes only the connections that live *solely* in this subtree — the
    ones that would otherwise land in Ungrouped. A connection that also
    belongs to another group is evidently still wanted there, and deleting a
    device out of "Core" because "Glasgow" was cleared out is the kind of
    surprise a destructive action must not spring. Those are untagged and
    counted under ``released`` as before.

    Returns ``key``, ``subgroups`` (how many went with it), ``released``
    (connections untagged and kept) and ``deleted`` (connections removed).
    """
    key = _key(key)
    prefix = f"{key}/"

    # Every group in the subtree, stored or implicit — a tag in use under the
    # prefix is a subgroup even if nobody ever gave it a colour.
    doomed = {key}
    for entry in _load():
        entry_key = entry.get("key") or ""
        if entry_key.startswith(prefix):
            doomed.add(entry_key)
    for tag in profiles_module.all_tags():
        if tag["tag"].startswith(prefix):
            doomed.add(tag["tag"])

    _save([g for g in _load() if g.get("key") not in doomed])

    # Decided here from the file, not from a list the interface sent: the
    # tree it was drawn from can be a refresh behind, and a connection tagged
    # into another group in the meantime must survive.
    deleted = (profiles_module.delete_solely_tagged(doomed)
               if delete_connections else 0)

    # One load and one save for the whole subtree (#327): a connection in
    # both the group and a subgroup counts once and is written once.
    released = profiles_module.retag_many({gone: "" for gone in doomed})
    return {"key": key, "released": released, "deleted": deleted,
            "subgroups": len(doomed) - 1}


@_synchronised
def set_order(keys: list[str]) -> list[dict]:
    """Store a hand-arranged order, front to back."""
    groups = _load()
    by_key = {g.get("key"): g for g in groups}
    # An order can name a group that no longer exists — the tree it was
    # dragged in can be a deletion behind. Creating an entry for it brought
    # the deleted group back as an empty ghost (#454). Only a group that is
    # still in use — a tag on some connection — earns a stored entry here.
    in_use = {t["tag"] for t in profiles_module.all_tags()}
    for position, name in enumerate(keys):
        key = _key(name)
        entry = by_key.get(key)
        if entry is None:
            if key not in in_use and not any(k.startswith(f"{key}/") for k in in_use):
                continue
            entry = {"id": str(uuid.uuid4()), "key": key, "name": key,
                     "colour": DEFAULT_COLOUR, "favourite": False}
            groups.append(entry)
            by_key[key] = entry
        entry["order"] = position
    _save(groups)
    return list_groups()


def set_membership(profile_id: str, key: str, member: bool) -> list[str]:
    """
    Put a connection in a group, or take it out.

    Adding, never moving: a connection belongs to as many groups as it has
    tags, which is the whole point of choosing tags over folders. Dragging one
    onto Production does not take it out of Glasgow.
    """
    key = _key(key)
    if not key:
        return []

    # By id from the cached list (#465): get_profiles() decorated every one
    # of five thousand connections with its credential state to find one.
    profile = profiles_module.find_profile(profile_id)
    if profile is None:
        raise ValueError("No such connection.")

    tags = profiles_module.normalise_tags(profile.get("tags"))
    if member and key not in tags:
        tags.append(key)
    elif not member and key in tags:
        tags.remove(key)
    return profiles_module.set_tags(profile_id, tags)


