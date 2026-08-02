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
import uuid
from typing import Any

from backend import paths, profiles as profiles_module

logger = logging.getLogger(__name__)

# Colours are named, not hex.
#
# A raw hex value chosen against the dark theme is unreadable in the light one,
# which is the exact bug test_contrast.py exists to prevent. These map to
# tokens the stylesheet defines twice, so a group looks deliberate in both.
COLOURS = (
    "slate", "blue", "green", "amber", "red", "purple", "teal", "pink",
)
DEFAULT_COLOUR = "slate"


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


def create_group(name: str, colour: str = DEFAULT_COLOUR) -> dict:
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
        _save(groups)
    else:
        groups.append({
            "id":        str(uuid.uuid4()),
            "key":       key,
            "name":      name.strip(),
            "colour":    colour if colour in COLOURS else DEFAULT_COLOUR,
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
            _retag(key, new_key)
            entry["key"] = new_key
        entry["name"] = new_name

    _save(groups)
    return get_group(entry["key"])


def delete_group(key: str) -> dict:
    """
    Remove a group. **The connections in it survive.**

    Nothing else on the dashboard destroys anything, so this removes the tag
    and the presentation and leaves every connection exactly where it was.
    The count is returned so the interface can say what it let go of rather
    than what it deleted.
    """
    key = _key(key)
    groups = [g for g in _load() if g.get("key") != key]
    _save(groups)
    released = _retag(key, "")
    return {"key": key, "released": released}


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


def _retag(old: str, new: str) -> int:
    """
    Rewrite one tag across every connection. Returns how many were touched.

    An empty `new` removes it. Goes through set_tags() rather than writing the
    file directly, so normalisation and the secret-stripping on save are the
    same ones every other path uses.
    """
    touched = 0
    for profile in profiles_module.get_profiles():
        tags = profiles_module.normalise_tags(profile.get("tags"))
        if old not in tags:
            continue
        tags = [new if t == old else t for t in tags] if new else \
               [t for t in tags if t != old]
        profiles_module.set_tags(profile["id"], tags)
        touched += 1
    return touched
