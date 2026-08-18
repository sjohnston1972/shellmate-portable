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

import json
import logging
import re
import uuid
from typing import Any

from backend import paths, profiles as profiles_module

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


def _file():
    return paths.data_dir() / "groups.json"


def _load() -> list[dict]:
    path = _file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        # A corrupt groups file must not take the dashboard down with it.
        # Presentation is the only thing lost; every group still exists,
        # because the membership lives on the connections.
        logger.warning("groups.json could not be read; falling back to defaults")
        return []


def _save(groups: list[dict]) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(groups, indent=2), encoding="utf-8")


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
        })

    # Favourites first, then the hand-arranged order, then alphabetically for
    # anything never dragged.
    out.sort(key=lambda g: (not g["favourite"], g["order"], g["name"].lower()))
    return out


def get_group(key: str) -> dict | None:
    key = _key(key)
    return next((g for g in list_groups() if g["key"] == key), None)


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


def set_order(keys: list[str]) -> list[dict]:
    """Store a hand-arranged order, front to back."""
    groups = _load()
    by_key = {g.get("key"): g for g in groups}
    for position, name in enumerate(keys):
        key = _key(name)
        entry = by_key.get(key)
        if entry is None:
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

    profile = next((p for p in profiles_module.get_profiles()
                    if p.get("id") == profile_id), None)
    if profile is None:
        raise ValueError("No such connection.")

    tags = profiles_module.normalise_tags(profile.get("tags"))
    if member and key not in tags:
        tags.append(key)
    elif not member and key in tags:
        tags.remove(key)
    return profiles_module.set_tags(profile_id, tags)


