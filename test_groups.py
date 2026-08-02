"""
test_groups.py — Group presentation: colours, icons, and the guessing.

Groups are tags with a face. The membership half is exercised through
test_profiles.py, because it *is* the tag machinery; what this covers is the
half a tag cannot carry, and in particular the icons added for #180.

The icons are worth their own test for a reason that is not obvious. The
Material Symbols font is subsetted at build time and a glyph outside the
subset renders as **its own name, in plain text, with nothing reporting an
error**. So the list has to be fixed, everything stored has to be in it, and
anything unrecognised has to fall back rather than render nothing.
test_icons.py holds the other end of that — that every name here is in the
shipped font.

    python test_groups.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

passed = 0
failed: list[str] = []

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-groups-"))

# Set before importing anything that resolves a path, and asserted rather than
# assumed: a mistake here writes into the user's real groups.json.
from backend import paths  # noqa: E402

paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import groups as gm  # noqa: E402
from backend import profiles as pm  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def _reset() -> None:
    for stem in ("groups.json", "profiles.json"):
        path = _TEMP / stem
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------

def test_the_icon_list_is_coherent() -> None:
    print("\n-- The icon list --")

    check("the default is one of the icons", gm.DEFAULT_ICON in gm.ICONS,
          f"{gm.DEFAULT_ICON!r} is not in ICONS")
    check("no icon is listed twice", len(set(gm.ICONS)) == len(gm.ICONS),
          "a duplicate would give the picker two identical cells")

    # Every word rule has to point at something that ships. A rule naming an
    # icon outside ICONS would guess a glyph the picker cannot offer and the
    # font may not carry.
    unknown = sorted({icon for _, icon in gm.ICON_WORDS if icon not in gm.ICONS})
    check("every word rule names an icon that exists", not unknown,
          f"these are guessed but not in ICONS: {', '.join(unknown)}")


# ---------------------------------------------------------------------------
# Guessing
# ---------------------------------------------------------------------------

def test_icons_are_guessed_from_words() -> None:
    print("\n-- Guessing an icon from a name --")

    for name, expected in (
        ("firewalls",        "security"),
        ("core switches",    "lan"),
        ("access switches",  "lan"),
        ("wan routers",      "router"),
        ("wireless",         "wifi"),
        ("out of band",      "cable"),
        ("load balancers",   "hub"),
        ("voice gateways",   "call"),
        ("management",       "settings"),
        ("site-004",         "apartment"),
    ):
        got = gm.guess_icon(name)
        check(f"{name!r} -> {expected}", got == expected, f"got {got!r}")


def test_guessing_matches_whole_words_only() -> None:
    """The failure mode the estate would have hit on every single group."""
    print("\n-- Whole words, not substrings --")

    # "site-004" contains "it"; "management" contains "man"; "switches"
    # contains "it" too. A substring match would have given the wrong icon to
    # essentially everything in a real estate.
    check("'site-004' is a site, not something containing 'it'",
          gm.guess_icon("site-004") == "apartment",
          f"got {gm.guess_icon('site-004')!r}")

    # Nothing recognisable falls back rather than guessing confidently wrong.
    for name in ("zzzz", "customer 91", "q3"):
        check(f"{name!r} falls back to a folder",
              gm.guess_icon(name) == gm.DEFAULT_ICON,
              f"got {gm.guess_icon(name)!r}")


def test_the_last_segment_decides() -> None:
    print("\n-- Nested names --")

    check("'site-004/firewalls' is a firewall, not a site",
          gm.guess_icon("site-004/firewalls") == "security",
          f"got {gm.guess_icon('site-004/firewalls')!r}")
    check("the site itself still reads as a site",
          gm.guess_icon("site-004") == "apartment")
    check("an empty name is a folder", gm.guess_icon("") == gm.DEFAULT_ICON)


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------

def test_a_new_group_gets_an_icon_without_being_asked() -> None:
    print("\n-- Creating --")
    _reset()

    group = gm.create_group("Firewalls", "red")
    check("created with a guessed icon", group["icon"] == "security",
          f"got {group.get('icon')!r}")
    check("and the colour asked for", group["colour"] == "red")

    chosen = gm.create_group("Widgets", "blue", icon="storage")
    check("an explicit icon wins over the guess", chosen["icon"] == "storage",
          f"got {chosen.get('icon')!r}")

    junk = gm.create_group("Nonsense", "blue", icon="gavel")
    check("an icon outside the list is refused, not stored",
          junk["icon"] == gm.DEFAULT_ICON,
          f"got {junk.get('icon')!r} — a glyph outside the subset renders as "
          f"its own name in plain text")


def test_an_icon_can_be_changed_and_survives_a_rename() -> None:
    print("\n-- Changing --")
    _reset()

    gm.create_group("Edge", "blue")
    changed = gm.update_group("edge", {"icon": "cloud"})
    check("the icon changes", changed["icon"] == "cloud",
          f"got {changed.get('icon')!r}")

    refused = gm.update_group("edge", {"icon": "gavel"})
    check("and an unknown one is ignored rather than stored",
          refused["icon"] == "cloud", f"got {refused.get('icon')!r}")

    # A rename rewrites the tag on every connection, and re-guessing would
    # quietly replace an icon somebody had settled on.
    renamed = gm.update_group("edge", {"name": "Firewalls"})
    check("a rename leaves a chosen icon alone", renamed["icon"] == "cloud",
          f"got {renamed.get('icon')!r} — renaming should not re-guess")


def test_a_group_that_predates_icons_still_has_one() -> None:
    """No migration: the fallback is computed, not written."""
    print("\n-- Groups with nothing stored --")
    _reset()

    import json
    (_TEMP / "groups.json").write_text(json.dumps([
        {"id": "1", "key": "wan routers", "name": "WAN routers",
         "colour": "amber", "favourite": False, "order": 1},
        {"id": "2", "key": "odd", "name": "Odd", "colour": "blue",
         "favourite": False, "order": 2, "icon": "gavel"},
    ]), encoding="utf-8")

    listed = {g["key"]: g for g in gm.list_groups()}
    check("an entry with no icon field is guessed one",
          listed["wan routers"]["icon"] == "router",
          f"got {listed['wan routers'].get('icon')!r}")
    check("an entry with a junk icon falls back",
          listed["odd"]["icon"] == gm.DEFAULT_ICON,
          f"got {listed['odd'].get('icon')!r}")


def test_an_implicit_group_gets_an_icon_too() -> None:
    """A tag nobody has styled is still a group, and still needs a face."""
    print("\n-- Tags that were never made into groups --")
    _reset()

    pm.save_profile({"name": "fw01", "hostname": "10.0.0.1", "port": 22,
                     "connection_type": "ssh", "username": "s",
                     "tags": ["firewalls"]})

    entry = next((g for g in gm.list_groups() if g["key"] == "firewalls"), None)
    check("the tag appears as a group", entry is not None)
    if entry:
        check("it is marked implicit", entry["implicit"] is True)
        check("and it has a guessed icon", entry["icon"] == "security",
              f"got {entry.get('icon')!r}")


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def test_a_nested_group_can_be_reached_over_the_api() -> None:
    """
    A subgroup's key contains a slash, and that broke every route it used.

    Nesting is expressed in the name — `site-004/firewalls` is a branch under
    `site-004` — so `encodeURIComponent` sends the key as `%2F`. The server
    decodes the path *before* routing, so `/api/groups/{key}` saw two segments
    and matched nothing: rename, recolour, pin, delete, membership and
    connect-all all returned 404 for any subgroup. Nothing said so. The
    interface applied the change locally, re-read the group, and showed the
    old value back.

    So this asks for the failure directly rather than trusting the routing
    table to keep meaning what it meant.
    """
    print("\n-- Subgroups over the API --")
    _reset()

    from urllib.parse import quote

    from fastapi.testclient import TestClient

    from backend.app import app

    gm.create_group("site-001")
    gm.create_group("site-001/firewalls")
    profile = pm.save_profile({"name": "fw01", "hostname": "10.0.0.9",
                               "port": 22, "connection_type": "ssh",
                               "username": "s"})

    client = TestClient(app)
    key = quote("site-001/firewalls", safe="")

    res = client.put(f"/api/groups/{key}", json={"colour": "teal"})
    check("a subgroup can be recoloured", res.status_code == 200,
          f"HTTP {res.status_code} — the slash in the key stopped it routing")

    res = client.put(f"/api/groups/{key}", json={"icon": "cloud"})
    check("and given an icon", res.status_code == 200
          and res.json().get("icon") == "cloud",
          f"HTTP {res.status_code}: {res.text[:120]}")

    res = client.post(f"/api/groups/{key}/members",
                      json={"profile_id": profile["id"], "member": True})
    check("a connection can be put into it", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:120]}")

    # Declared before the greedy match, and it has to stay that way — the
    # other way round, this renames a group called "order" and retags
    # everything in it.
    res = client.put("/api/groups/order", json={"keys": ["site-001"]})
    check("the order route still wins over the key route",
          res.status_code == 200 and "groups" in res.json(),
          f"HTTP {res.status_code}: {res.text[:120]}")

    res = client.delete(f"/api/groups/{key}")
    check("and it can be deleted", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:120]}")

    # A flat key must not have regressed on the way past.
    res = client.put("/api/groups/site-001", json={"colour": "red"})
    check("a top-level group still works", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:120]}")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Groups")
    print("=" * 52)

    tests = (
        test_the_icon_list_is_coherent,
        test_icons_are_guessed_from_words,
        test_guessing_matches_whole_words_only,
        test_the_last_segment_decides,
        test_a_new_group_gets_an_icon_without_being_asked,
        test_an_icon_can_be_changed_and_survives_a_rename,
        test_a_group_that_predates_icons_still_has_one,
        test_an_implicit_group_gets_an_icon_too,
        test_a_nested_group_can_be_reached_over_the_api,
    )
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for line in failed:
            print(f"  {line}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TEMP, ignore_errors=True)
