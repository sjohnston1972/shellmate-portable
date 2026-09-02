"""
test_accessibility.py — The baseline a keyboard or a screen reader needs (#428).

Static, like test_tooltips and test_contrast: the markup and scripts are
read as text and the things that make the interface operable without a
mouse are asserted present. Each one is a thing that was missing.

    python test_accessibility.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
JS = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / "frontend" / "js").glob("*.js")}

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


def test_landmarks_and_roles() -> None:
    print("\n-- Landmarks and roles --")
    check("the tab strip is a tablist", 'id="tab-list" role="tablist"' in HTML)
    check("  with a name", 'aria-label="Open sessions"' in HTML)
    check("tabs are tabs", "setAttribute('role', 'tab')" in JS["tabs.js"])
    check("  and say which is selected", "aria-selected" in JS["tabs.js"])
    check("  and only the active one is in the tab order", "tabIndex = i === index ? 0 : -1" in JS["tabs.js"])
    check("the sidebar nav is named", 'id="sidebar-nav" aria-label=' in HTML)
    check("the status bar is a status region", 'id="status-bar" role="status"' in HTML)
    check("toasts are a polite live region",
          "setAttribute('aria-live', 'polite')" in JS["alerts.js"] and "setAttribute('role', 'status')" in JS["alerts.js"])
    check("context menus carry menu roles",
          "setAttribute('role', 'menu')" in JS["menu.js"] and "setAttribute('role', 'menuitem')" in JS["menu.js"])
    check("the tab palette is a listbox", "setAttribute('role', 'listbox')" in JS["palette.js"])


def test_keyboard() -> None:
    print("\n-- Keyboard --")
    check("arrow keys move between tabs", "e.key === 'ArrowRight' || e.key === 'ArrowLeft'" in JS["tabs.js"])
    check("Enter or Space activates a tab", "e.key === 'Enter' || e.key === ' '" in JS["tabs.js"])
    check("the close button on a tab has an accessible name", "closeBtn.setAttribute('aria-label'" in JS["tabs.js"])
    check("menus take arrow keys, Home/End and Escape",
          all(k in JS["menu.js"] for k in ("'ArrowDown'", "'ArrowUp'", "'Home'", "'End'", "'Escape'")))
    check("menus restore focus on close", "restoreFocus" in JS["menu.js"])
    check("dialogs trap Escape and Enter", "e.key === 'Escape'" in JS["dialog.js"] and "e.key === 'Enter'" in JS["dialog.js"])


def test_focus_and_contrast_modes() -> None:
    print("\n-- Focus ring and forced colours --")
    check("a global focus-visible ring exists",
          re.search(r"^:focus-visible\s*\{[^}]*outline:\s*2px solid", CSS, re.M) is not None)
    check("forced-colors mode is handled", "@media (forced-colors: active)" in CSS)
    check("  with real borders on the surfaces that were only shades",
          "border: 1px solid CanvasText" in CSS)
    check("icon-only buttons in markup carry aria-labels",
          HTML.count('class="icon-btn" aria-label=') >= 5, str(HTML.count('class="icon-btn" aria-label=')))


def main() -> int:
    print("=" * 52)
    print("  Accessibility baseline")
    print("=" * 52)
    for test in (test_landmarks_and_roles, test_keyboard, test_focus_and_contrast_modes):
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
