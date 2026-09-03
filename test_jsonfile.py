"""
test_jsonfile.py — The JSON data-file helper keeps files intact (#457).

Forty writers at once, a corrupt file, a write that fails half way: none of
them may cost the user a file. Run: python test_jsonfile.py
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import jsonfile  # noqa: E402

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


def test_concurrent_writers_lose_nothing(folder: Path) -> None:
    print("\n-- Forty writers, one file --")
    path = folder / "profiles.json"
    jsonfile.write(path, [])
    errors: list[str] = []

    def add(n: int) -> None:
        try:
            with jsonfile.locked(path):
                items = jsonfile.read(path, [], expect=list)
                items.append({"id": n})
                jsonfile.write(path, items)
        except Exception as exc:                       # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=add, args=(n,)) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(path.read_text(encoding="utf-8"))
    check("no writer raised", not errors, str(errors[:3]))
    check("every edit survived", sorted(d["id"] for d in data) == list(range(40)), str(len(data)))
    check("no temp files were left behind", not list(folder.glob(".profiles.json.*.tmp")),
          str(list(folder.iterdir())))


def test_corrupt_file_is_set_aside(folder: Path) -> None:
    print("\n-- A corrupt file --")
    path = folder / "groups.json"
    path.write_text("[{\"key\": \"site\"", encoding="utf-8")       # truncated mid-write
    data = jsonfile.read(path, [], expect=list)
    aside = list(folder.glob("groups.json.corrupt-*"))
    check("the default comes back", data == [])
    check("the bad file is kept beside the good one", len(aside) == 1, str(list(folder.iterdir())))
    check("  and the original name is free", not path.exists())
    jsonfile.write(path, [{"key": "site"}])
    check("a later save does not overwrite the evidence",
          len(list(folder.glob("groups.json.corrupt-*"))) == 1 and json.loads(path.read_text()) == [{"key": "site"}])

    wrong = folder / "sets.json"
    wrong.write_text("{\"not\": \"a list\"}", encoding="utf-8")
    check("the wrong shape is treated the same way",
          jsonfile.read(wrong, [], expect=list) == [] and list(folder.glob("sets.json.corrupt-*")))


def test_write_is_atomic(folder: Path) -> None:
    print("\n-- Atomic writes --")
    path = folder / "settings.json"
    jsonfile.write(path, {"a": 1})

    class Boom(Exception):
        pass

    class Explosive(dict):
        pass

    # json.dumps of an unserialisable value raises before any byte reaches
    # the target, so the old file must be untouched and nothing left over.
    try:
        jsonfile.write(path, {"a": object()})
    except TypeError:
        pass
    check("a failed write leaves the old file intact", json.loads(path.read_text()) == {"a": 1})
    check("  and no temp file", not list(folder.glob(".settings.json.*.tmp")))
    jsonfile.write(path, {"a": 2}, indent=None)
    check("compact output when asked", path.read_text() == '{"a":2}', path.read_text())
    check("a missing file reads as the default", jsonfile.read(folder / "nope.json", {"x": 1}) == {"x": 1})


def main() -> int:
    print("=" * 52)
    print("  jsonfile")
    print("=" * 52)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for test in (test_concurrent_writers_lose_nothing, test_corrupt_file_is_set_aside, test_write_is_atomic):
            try:
                test(folder)
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
