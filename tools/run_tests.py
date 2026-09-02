"""
run_tests.py — Run every test_*.py and say, once, whether they all passed.

The tests are standalone scripts on purpose: each one sets up its own
temporary data directory before importing the backend, and several start a
real server on a spare port. Collecting them into one pytest process would
have them fighting over `paths._data_dir_cache` and module state, so the
runner keeps the shape they already have and adds the two things that were
missing (#422): one exit code for all of them, and a summary that names the
failures rather than scrolling past them.

    python tools/run_tests.py                 # everything
    python tools/run_tests.py vault keys      # only test_vault.py, test_keys.py
    python tools/run_tests.py --skip phase2,caching

`SHELLMATE_SKIP_TESTS` does the same as --skip, for a CI job on a machine
without the Playwright browsers.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    skip: set[str] = set()
    names: list[str] = []
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--skip":
            skip.update(_stems(args.pop(0) if args else ""))
        elif arg.startswith("--skip="):
            skip.update(_stems(arg.split("=", 1)[1]))
        else:
            names.append(_stem(arg))
    skip.update(_stems(os.environ.get("SHELLMATE_SKIP_TESTS", "")))

    files = sorted(ROOT.glob("test_*.py"))
    if names:
        files = [f for f in files if f.stem in names]
    files = [f for f in files if f.stem not in skip]
    if not files:
        print("No tests selected.")
        return 2

    results: list[tuple[str, int, float]] = []
    for path in files:
        print(f"\n===== {path.name} =====", flush=True)
        started = time.monotonic()
        # Each test in its own interpreter, from the repo root, with UTF-8
        # output so a check that prints an arrow or an em-dash cannot itself
        # crash the run on a cp1252 console.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        code = subprocess.call([sys.executable, str(path)], cwd=str(ROOT), env=env)
        results.append((path.name, code, time.monotonic() - started))

    failed = [(n, c) for n, c, _ in results if c != 0]
    print("\n" + "=" * 60)
    for name, code, seconds in results:
        mark = "ok  " if code == 0 else f"FAIL"
        print(f"  {mark}  {name:32s} {seconds:6.1f}s")
    print("=" * 60)
    print(f"  {len(results) - len(failed)} of {len(results)} test files passed")
    if failed:
        print("  failed: " + ", ".join(n for n, _ in failed))
    return 1 if failed else 0


def _stem(name: str) -> str:
    name = name.strip()
    if name.endswith(".py"):
        name = name[:-3]
    if not name.startswith("test_"):
        name = "test_" + name
    return name


def _stems(text: str) -> set[str]:
    return {_stem(part) for part in text.split(",") if part.strip()}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
