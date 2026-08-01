"""
test_snippets.py — The command library, once it was generated rather than typed.

The shipped library used to be eighteen entries, fourteen of them Cisco IOS.
In the mixed estate ShellMate is built for, most of it applied to none of the
devices in front of you.

Writing the same intents out for seven platforms by hand is how a library ends
up wrong: ``platforms.py`` already knows that "interfaces" is
``show ip interface brief`` on IOS and ``show interfaces terse`` on Junos, and
a second copy of that knowledge in another file disagrees with it eventually.
So they are derived. These tests hold the properties that makes safe:

  - nothing generated writes to a device
  - every generated command really is the platform's own
  - a platform somebody adds gets a library without any code change
  - growing the shipped set reaches people who already have a snippets.json,
    without resurrecting entries they deleted on purpose

    python test_snippets.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-snippets-"))
paths._data_dir_cache = _TEMP

from backend import platforms as platforms_module          # noqa: E402
from backend import snippets                              # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def fresh() -> None:
    snippets.snippets_path().unlink(missing_ok=True)


def test_coverage() -> None:
    print("\n-- What ships --")
    fresh()
    library = snippets.load_snippets()

    check("the library is substantially bigger than the hand-written set",
          len(library) > 100, f"got {len(library)}")

    platforms = {s.platform for s in library if s.platform}
    expected = {"ios", "nxos", "asa", "junos", "panos", "arista", "linux"}
    check("every platform ShellMate knows has entries",
          expected <= platforms, f"missing {sorted(expected - platforms)}")

    check("and something applies to any device at all",
          any(not s.platform for s in library))

    ids = [s.id for s in library]
    check("ids are unique", len(ids) == len(set(ids)),
          f"{len(ids) - len(set(ids))} duplicate(s)")


def test_nothing_destructive_is_generated() -> None:
    """A shipped library is not the place for `write erase` one click away."""
    print("\n-- Nothing destructive --")
    generated = snippets.generated_snippets()

    check("no generated entry writes to a device",
          not any(s.writes for s in generated),
          "a generated snippet claims to write")

    dangerous = ("erase", "reload", "delete", "format", "shutdown",
                 "zeroize", "rm -rf", "write memory", "commit")
    offenders = [
        f"{s.id}: {c}"
        for s in generated for c in s.commands
        if any(word in c.lower() for word in dangerous)
    ]
    check("and none of them contains a destructive command",
          not offenders, "; ".join(offenders))


def test_commands_come_from_the_alias_table() -> None:
    """The whole point: one source of truth, so they cannot disagree."""
    print("\n-- Derived, not copied --")
    profiles = platforms_module.load_profiles()
    mismatched = []

    for snippet in snippets.generated_snippets():
        if snippet.id.endswith("-health"):
            continue
        alias = snippet.id.rsplit("-", 1)[-1]
        expected = profiles[snippet.platform].aliases.get(alias)
        if expected != snippet.commands[0]:
            mismatched.append(f"{snippet.id}: {snippet.commands[0]!r} != {expected!r}")

    check("every generated command is the platform's own alias",
          not mismatched, "; ".join(mismatched[:3]))

    ios = {s.id: s for s in snippets.generated_snippets() if s.platform == "ios"}
    junos = {s.id: s for s in snippets.generated_snippets() if s.platform == "junos"}
    check("the same intent differs by platform, as it must",
          ios["gen-ios-ints"].commands != junos["gen-junos-ints"].commands,
          "two platforms produced the same command for interfaces")


def test_a_new_platform_gets_a_library() -> None:
    print("\n-- A platform somebody added --")
    platforms_module.save_profile_edits("acme", {
        "id": "acme", "name": "Acme Switch",
        "aliases": {"ver": "display version", "ints": "display interface brief"},
    })
    try:
        generated = [s for s in snippets.generated_snippets() if s.platform == "acme"]
        check("it gets snippets with no code change", len(generated) >= 2,
              f"got {len(generated)}")
        check("built from its own commands",
              any(s.commands == ["display version"] for s in generated))
    finally:
        platforms_module.delete_platform("acme")


def test_growing_the_library() -> None:
    """
    A library that cannot grow is the failure mode this guards.

    A built-in the user deleted must stay deleted; one that did not exist when
    their file was written must appear. Telling those apart needs a record of
    what has been offered, which is what ``known_builtins`` is for.
    """
    print("\n-- Growing it under an existing file --")
    fresh()
    snippets.load_snippets()

    document = json.loads(snippets.snippets_path().read_text(encoding="utf-8"))
    check("the file records what has been offered",
          bool(document.get("known_builtins")), "no known_builtins written")

    # Delete one deliberately.
    snippets.delete_snippet("gen-ios-ver")
    after = [s.id for s in snippets.load_snippets()]
    check("a deleted built-in goes", "gen-ios-ver" not in after)

    # And stays gone across a reload — the annoying failure would be its
    # return every time the application starts.
    again = [s.id for s in snippets.load_snippets()]
    check("and stays gone", "gen-ios-ver" not in again)

    # Saving an unrelated snippet must not undo the deletion either. Every
    # write has to carry the offered-ids record forward, not just the delete.
    snippets.save_snippet({"name": "Something of my own", "commands": ["show clock"]})
    after_save = [s.id for s in snippets.load_snippets()]
    check("and saving something else does not bring it back",
          "gen-ios-ver" not in after_save,
          "an unrelated save resurrected a deleted built-in")

    # Now simulate a release that ships something new: forget one id.
    document = json.loads(snippets.snippets_path().read_text(encoding="utf-8"))
    document["known_builtins"] = [i for i in document["known_builtins"]
                                  if i != "gen-junos-bgp"]
    document["snippets"] = [s for s in document["snippets"]
                            if s["id"] != "gen-junos-bgp"]
    snippets.snippets_path().write_text(json.dumps(document), encoding="utf-8")

    ids = [s.id for s in snippets.load_snippets()]
    check("a genuinely new built-in is added to an existing library",
          "gen-junos-bgp" in ids, "the new entry never reached the user")
    check("without resurrecting the one that was deleted",
          "gen-ios-ver" not in ids, "a deliberate deletion came back")


def test_old_format_file() -> None:
    print("\n-- A file from before the format changed --")
    fresh()
    snippets.snippets_path().write_text(json.dumps([{
        "id": "mine", "name": "My own", "commands": ["show version"],
    }]), encoding="utf-8")

    library = snippets.load_snippets()
    ids = [s.id for s in library]
    check("the user's own entry survives migration", "mine" in ids)
    check("and the new built-ins arrive", len(library) > 100, f"got {len(library)}")

    document = json.loads(snippets.snippets_path().read_text(encoding="utf-8"))
    check("the file is rewritten in the new shape",
          isinstance(document, dict) and "known_builtins" in document)


def test_broken_file() -> None:
    print("\n-- A file broken by hand --")
    for label, content in (
        ("unparseable JSON", "{ not json"),
        ("the wrong shape",  json.dumps("a string")),
    ):
        snippets.snippets_path().write_text(content, encoding="utf-8")
        library = snippets.load_snippets()
        check(f"{label}: falls back to the shipped library", len(library) > 100,
              f"got {len(library)}")


def test_reset() -> None:
    print("\n-- Reset --")
    fresh()
    snippets.load_snippets()
    snippets.delete_snippet("gen-ios-ver")
    restored = snippets.reset_to_defaults()
    check("reset brings back everything, including generated entries",
          any(s.id == "gen-ios-ver" for s in restored))
    check("and it matches the shipped set exactly",
          {s.id for s in restored} == {s.id for s in snippets.all_builtins()})


def main() -> int:
    print("\n" + "=" * 52)
    print("  Command library")
    print("=" * 52)

    for test in (
        test_coverage,
        test_nothing_destructive_is_generated,
        test_commands_come_from_the_alias_table,
        test_a_new_platform_gets_a_library,
        test_growing_the_library,
        test_old_format_file,
        test_broken_file,
        test_reset,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_TEMP, ignore_errors=True)

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
