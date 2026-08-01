"""
test_legal.py — The attributions match what is actually shipped.

An attribution list is only worth anything if it is true, and the way it stops
being true is quiet: somebody adds a dependency, the executable grows a library,
and the page still lists the set from six months ago. It then reads as a
checked list, which is worse than no list at all.

So this compares three things that must agree:

    what tools/collect_licences.py says it collected  (MANIFEST.json)
    what is actually on disk to be shipped            (licences/*.txt)
    what the manual page tells the reader             (legal.md)

And it holds the two claims on that page that are not merely tidiness: that
ShellMate is a proprietary product whose recipient is told what they may do
with it, and that the LGPL components are named with a route to replacing
them. Both are distribution obligations, not documentation preferences.

    python test_legal.py
"""

import json
import sys
from pathlib import Path

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
DOCS = ROOT / "frontend" / "docs"
LICENCES = DOCS / "licences"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def manifest() -> list[dict]:
    return json.loads((LICENCES / "MANIFEST.json").read_text(encoding="utf-8"))["components"]


def test_every_component_ships_its_licence() -> None:
    """
    Named is not the same as shipped.

    Every licence here requires its text to travel with the software — MIT and
    BSD say so as plainly as the LGPL does, and the OFL is explicit that it
    must accompany the font files. Before this, `vendor_assets.py` downloaded
    the .woff2 files and nothing else.
    """
    print("\n-- Every component's licence text --")

    check("the manifest exists", (LICENCES / "MANIFEST.json").exists(),
          "run: python tools/collect_licences.py")
    if not (LICENCES / "MANIFEST.json").exists():
        return

    components = manifest()
    check("it lists a substantial set", len(components) > 30,
          f"only {len(components)} — a dependency has probably been missed")

    for component in components:
        name = component["component"]
        path = LICENCES / (component["file"] or "")
        check(f"{name}: its licence text is on disk",
              bool(component["file"]) and path.is_file(),
              "named in the manifest with no text to accompany it")
        if path.is_file():
            check(f"{name}: and is not empty", path.stat().st_size > 200,
                  f"{path.stat().st_size} bytes")


def test_nothing_is_shipped_without_being_declared() -> None:
    """The other direction: a stray file in licences/ that nothing accounts for."""
    print("\n-- Nothing unaccounted for --")
    declared = {c["file"] for c in manifest()} | {"MANIFEST.json"}
    on_disk = {p.name for p in LICENCES.iterdir() if p.is_file()}
    stray = on_disk - declared
    check("every file in licences/ is in the manifest", not stray,
          f"orphaned: {', '.join(sorted(stray))}")


def test_the_page_lists_what_the_manifest_holds() -> None:
    """The manual is where a user looks. It has to match."""
    print("\n-- The manual page --")

    page = DOCS / "legal.md"
    check("the page exists", page.exists(),
          "run: python tools/collect_licences.py")
    if not page.exists():
        return

    text = page.read_text(encoding="utf-8")
    for component in manifest():
        check(f"{component['component']}: appears on the page",
              component["component"] in text,
              "bundled, and the reader is not told")


def test_the_page_is_in_the_manual() -> None:
    """
    Registered in docs.js, not merely present in the folder.

    The bundled manual is the only documentation available offline, and an
    attribution nobody can reach has not really been given.
    """
    print("\n-- Reachable from the manual --")
    docs_js = (ROOT / "frontend" / "js" / "docs.js").read_text(encoding="utf-8")
    check("legal.md is in PAGES", "legal.md" in docs_js,
          "the file exists but nothing links to it")


def test_what_the_page_has_to_say() -> None:
    """
    The claims that are obligations rather than tidiness.

    Two of them. A recipient of the executable has to be able to find out what
    they may do with it — that was the largest gap, since ShellMate's own
    licence was stated nowhere at all. And the LGPL components have to be
    named, with their licence text and a route to replacing them.
    """
    print("\n-- The statements that have to be there --")
    text = (DOCS / "legal.md").read_text(encoding="utf-8")

    check("ShellMate's owner is named",
          "Foundry Networks and Services" in text)
    check("there is a contact address", "support@foundry-ns.com" in text)
    check("a copyright line", "Copyright" in text and "©" in text)
    check("and what a recipient may do with it",
          "proprietary" in text.lower(),
          "ShellMate's own licence was stated nowhere, which is the gap that "
          "mattered most")

    check("the warranty is disclaimed",
          '"as is"' in text and "without warranty" in text.lower())
    check("and it is specific about the equipment",
          "responsible for what is sent to your equipment" in text,
          "generic boilerplate, for a tool that types into production devices")

    check("what is sent to AI providers is stated",
          "Anthropic" in text and "leave" in text.lower(),
          "terminal output goes to a third party and the manual has to say so")

    for library in ("paramiko", "pystray"):
        check(f"the LGPL component {library} is named", library in text)
    check("the LGPL is addressed rather than listed",
          "LGPL" in text and "unmodified" in text,
          "naming them satisfies notice, not the replaceability condition")
    check("and there is a route to replacing them",
          "folder build" in text,
          "the recipient must be able to substitute their own build")

    check("the page says it is not legal advice",
          "not legal advice" in text.lower(),
          "engineers wrote it; that should be on the page")


def test_the_modified_font_carries_its_notice() -> None:
    """
    Material Symbols is subsetted, which makes it a modified work.

    Apache 2.0 permits that with a notice of modification. Nothing under the
    OFL is subsetted, and nothing under it can be while it keeps a Reserved
    Font Name — so this is also a check that the constraint has not been
    forgotten the next time something is shrunk.
    """
    print("\n-- The font ShellMate modifies --")
    components = {c["component"]: c for c in manifest()}

    symbols = components.get("Material Symbols Outlined")
    check("Material Symbols is attributed", symbols is not None)
    if symbols:
        check("it is marked as modified", bool(symbols["modified"]),
              "it is subsetted at build time, which makes it a modified work")
        check("under a licence that permits that with notice",
              "Apache" in symbols["licence"], symbols["licence"])

    ofl = [c for c in components.values()
           if "Open Font" in c["licence"] and c["modified"]]
    check("nothing under the OFL is modified", not ofl,
          f"{', '.join(c['component'] for c in ofl)} — an OFL font with a "
          f"Reserved Font Name cannot be modified and keep its name")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Legal and licences")
    print("=" * 52)

    for test in (test_every_component_ships_its_licence,
                 test_nothing_is_shipped_without_being_declared,
                 test_the_page_lists_what_the_manifest_holds,
                 test_the_page_is_in_the_manual,
                 test_what_the_page_has_to_say,
                 test_the_modified_font_carries_its_notice):
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
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
