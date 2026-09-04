"""
test_inventory.py — What a device says about itself is kept against the
saved connection (#536): version, model, serial, and when it was last
opened. Searchable, exportable, and never invented.

Run: python test_inventory.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-inventory-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import configs, fingerprint, profiles  # noqa: E402

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


SHOW_VERSION = """Cisco IOS Software, C3850 Software (CAT3K_CAA-UNIVERSALK9-M), Version 15.2(7)E3, RELEASE SOFTWARE
switch uptime is 4 weeks, 2 days
Model Number   : WS-C3850-48P
System Serial Number : FCW2140L0GH
"""


def test_version_extraction() -> None:
    print("\n-- A version out of what the device printed --")
    check("the release is read from show version",
          fingerprint.version_from(SHOW_VERSION) == "15.2(7)E3",
          fingerprint.version_from(SHOW_VERSION))
    check("  and narrowed by platform where it matters",
          fingerprint.version_from(SHOW_VERSION, "ios") == "15.2(7)E3")
    check("nothing in, nothing out", fingerprint.version_from("") == ""
          and fingerprint.version_from("hello") == "")


def test_rows_to_facts() -> None:
    print("\n-- Parsed rows become facts --")
    rows = [{"version": "15.2(7)E3", "hardware": ["WS-C3850-48P"], "serial": ["FCW2140L0GH"]}]
    check("a version column is found", configs._first_value(rows, ("version", "os_version")) == "15.2(7)E3")
    check("a list column takes its first value",
          configs._first_value(rows, ("model", "hardware")) == "WS-C3850-48P")
    check("a missing column is empty, not an error", configs._first_value(rows, ("nope",)) == "")
    check("no rows is empty too", configs._first_value([], ("serial",)) == "")


def test_recording() -> None:
    print("\n-- Kept against the saved connection --")
    saved = profiles.save_profile({"name": "sw1", "hostname": "10.1.1.1", "port": 22,
                                   "username": "eng", "connection_type": "ssh"})
    check("a profile to hang them on", bool(saved.get("id")))

    changed = profiles.record_inventory("10.1.1.1", 22, "eng", {
        "version": "15.2(7)E3", "model": "WS-C3850-48P", "serial": "FCW2140L0GH",
        "last_seen_platform": "ios"})
    kept = profiles.find_profile(saved["id"])
    check("the facts are written", changed and kept.get("model") == "WS-C3850-48P", str(kept))
    check("  all of them", kept.get("version") == "15.2(7)E3" and kept.get("serial") == "FCW2140L0GH")

    profiles.record_inventory("10.1.1.1", 22, "eng", {"serial": ""})
    kept = profiles.find_profile(saved["id"])
    check("an empty value does not erase what was known",
          kept.get("serial") == "FCW2140L0GH", str(kept.get("serial")))

    profiles.record_inventory("10.1.1.1", 22, "eng", {"name": "hacked", "password": "x"})
    kept = profiles.find_profile(saved["id"])
    check("only inventory fields are accepted",
          kept.get("name") == "sw1" and "password" not in kept, str(kept))

    profiles.record_inventory("10.9.9.9", 22, "eng", {"model": "elsewhere"})
    kept = profiles.find_profile(saved["id"])
    check("another device's facts land elsewhere", kept.get("model") == "WS-C3850-48P")

    profiles.record_connected("10.1.1.1", 22, "eng")
    kept = profiles.find_profile(saved["id"])
    check("the connection time is recorded", bool(kept.get("last_connected")), str(kept.get("last_connected")))

    print("\n-- And they leave in the export --")
    text = profiles.export_csv() if hasattr(profiles, "export_csv") else ""
    check("the export carries the columns",
          all(c in profiles.CSV_COLUMNS for c in ("version", "model", "serial", "last_connected")),
          str(profiles.CSV_COLUMNS))
    if text:
        check("  and this device's values", "WS-C3850-48P" in text and "FCW2140L0GH" in text,
              text.splitlines()[0] if text else "")
    check("but they are never imported — only a device may state them",
          "version" not in profiles._CSV_ALIASES and "serial" not in profiles._CSV_ALIASES)


def main() -> int:
    print("=" * 52)
    print("  Inventory facts")
    print("=" * 52)
    for test in (test_version_extraction, test_rows_to_facts, test_recording):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: {exc!r}")
            print(f"  FAIL {test.__name__}: {exc!r}")
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("FAILURES:")
        for f in failed:
            print(" -", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
