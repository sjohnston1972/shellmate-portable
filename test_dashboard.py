"""
test_dashboard.py — The dashboard actually covers the terminals.

The dashboard is an overlay in front of the terminals, not a replacement for
them — sessions must keep running underneath. That makes covering them a
stacking problem, and stacking bugs are invisible in source review: a tiled
layout's purple focus ring sat at z-index 20, escaped its container, and
painted straight through the dashboard (#212).

Two promises are held here, both read out of the source because the failure
has no runtime error to catch:

- style.css isolates #terminals-container, so nothing inside it — whatever
  z-index it carries — can out-stack a sibling.
- tabs.js reveals the welcome screen only through showDashboard(), which is
  the one place that also puts the terminals behind it and tells the alert
  stack (#168). A bare classList.remove('hidden') anywhere else recreates
  the bug.

    python test_dashboard.py
"""

import re
import sys
from pathlib import Path

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


def _rule_block(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly this."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else ""


def test_terminals_cannot_outstack_the_dashboard() -> None:
    print("\n-- Stacking --")
    css = (Path(__file__).parent / "frontend" / "css" / "style.css") \
        .read_text(encoding="utf-8")

    block = _rule_block(css, "#terminals-container")
    check("the terminals container is findable", bool(block),
          "#terminals-container rule moved or was renamed — this test is blind")
    check("the container is a stacking context of its own",
          "isolation: isolate" in block,
          "without isolation the focus ring's z-index escapes to the root "
          "and paints over the dashboard")

    check("the focus ring this guards against still exists",
          ".pane-focused::after" in css,
          "the ring is gone — this test may be guarding nothing, review it")


def test_every_reveal_takes_the_full_path() -> None:
    print("\n-- Revealing the dashboard --")
    source = (Path(__file__).parent / "frontend" / "js" / "tabs.js") \
        .read_text(encoding="utf-8")

    reveals = re.findall(r"welcomeScreen\.classList\.remove\('hidden'\)", source)
    check("the welcome screen is un-hidden in exactly one place",
          len(reveals) == 1,
          f"{len(reveals)} sites remove 'hidden' — every one that is not "
          f"showDashboard() shows the dashboard without hiding the terminals")

    show = re.search(r"function showDashboard\(\)\s*\{(.*?)\n  \}", source, re.S)
    check("showDashboard is findable", show is not None)
    if show:
        check("that one place is showDashboard itself",
              "welcomeScreen.classList.remove('hidden')" in show.group(1))
        check("which puts the terminals behind the dashboard",
              "behind-dashboard" in show.group(1))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Dashboard")
    print("=" * 52)

    for test in (
        test_terminals_cannot_outstack_the_dashboard,
        test_every_reveal_takes_the_full_path,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__} raised {exc!r}")
            print(f"  FAIL {test.__name__} raised\n       {exc!r}")

    print("\n" + "-" * 52)
    if failed:
        print(f"  {passed} passed, {len(failed)} FAILED")
        for line in failed:
            print(f"    - {line}")
    else:
        print(f"  all {passed} checks passed")
    print("-" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
