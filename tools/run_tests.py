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

Each file gets `SHELLMATE_TEST_TIMEOUT` seconds (600 by default). One that
overruns is killed and named, so a hung test costs ten minutes and a line
saying which file, not the whole CI job and no line at all (#517).

**A file that fails keeps its output** (#586). The summary line is enough
for a test that fails every time and useless for one that fails now and
then: by the time anybody looks, the run is gone and all that is left is a
name. So a failing file's output is written to `.test-failures/`, named
after the file and the moment, and the last lines of it are repeated in the
summary — a CI job keeps no artefacts but does keep its log. Nothing is
kept for a file that passes; the directory stays empty on a good run.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The exit code recorded for a file that had to be killed.
TIMED_OUT = -9

#: Where a failing file's output is kept. Beside the repo rather than in
#: the data directory: this is evidence about a test run, not application
#: state, and somebody chasing an intermittent wants it where the code is.
EVIDENCE = ROOT / ".test-failures"

#: How much of a failure to repeat in the summary. Enough to carry the
#: failing checks and the count, not so much that a genuinely broken file
#: buries the summary it is part of.
TAIL_LINES = 25


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("SHELLMATE_TEST_TIMEOUT", "600")))
    except ValueError:
        return 600.0


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
    kept: dict[str, Path] = {}
    output: dict[str, str] = {}
    for path in files:
        print(f"\n===== {path.name} =====", flush=True)
        started = time.monotonic()
        limit = _timeout()
        code, text = _run(path, limit)
        results.append((path.name, code, time.monotonic() - started))
        if code != 0:
            output[path.name] = text
            saved = _keep(path.name, text, code, limit)
            if saved:
                kept[path.name] = saved

    failed = [(n, c) for n, c, _ in results if c != 0]
    print("\n" + "=" * 60)
    for name, code, seconds in results:
        mark = "ok  " if code == 0 else ("HUNG" if code == TIMED_OUT else "FAIL")
        print(f"  {mark}  {name:32s} {seconds:6.1f}s")
    print("=" * 60)
    print(f"  {len(results) - len(failed)} of {len(results)} test files passed")
    if failed:
        print("  failed: " + ", ".join(n + (" (killed)" if c == TIMED_OUT else "") for n, c in failed))
        # The evidence, repeated where a CI log will carry it. A run that
        # fails once in twenty is the reason this exists: the next
        # occurrence has to arrive with more than its own name.
        for name, _ in failed:
            print("\n" + "-" * 60)
            where = kept.get(name)
            print(f"  {name} — last {TAIL_LINES} lines"
                  + (f", kept in full at {where}" if where else ""))
            print("-" * 60)
            for line in output.get(name, "").splitlines()[-TAIL_LINES:]:
                print("  | " + line)
    return 1 if failed else 0


def _run(path: Path, limit: float) -> tuple[int, str]:
    """
    One test file, streamed as it goes and kept as it goes.

    Streamed rather than captured and printed at the end, because a suite
    that shows nothing for ten minutes is one nobody runs locally; kept as
    well, because a failure that has scrolled past is a failure with no
    evidence. Each test runs in its own interpreter, from the repo root,
    with UTF-8 output so a check that prints an arrow or an em-dash cannot
    itself crash the run on a cp1252 console.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    lines: list[str] = []
    process = subprocess.Popen(
        [sys.executable, str(path)], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    try:
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            print(line, end="", flush=True)
        code = process.wait(timeout=max(1.0, limit))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        print(f"\n!!! {path.name} did not finish within {limit:.0f}s and was killed",
              flush=True)
        lines.append(f"!!! killed after {limit:.0f}s")
        code = TIMED_OUT
    return code, "\n".join(lines)


def _keep(name: str, text: str, code: int, limit: float) -> Path | None:
    """
    Write a failing file's output where the next person can read it.

    Best-effort: a runner that cannot write its evidence still has to
    report the failure. Losing the run over a read-only checkout would be
    the failure mode this was meant to prevent, inverted.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        where = EVIDENCE / f"{name[:-3]}-{stamp}.log"
        where.write_text(
            f"{name}\n"
            f"when:      {datetime.now().isoformat(timespec='seconds')}\n"
            f"exit code: {code}"
            + (f" (killed after {limit:.0f}s)" if code == TIMED_OUT else "")
            + f"\npython:    {sys.version.split()[0]}\n"
            + "-" * 60 + "\n" + text + "\n",
            encoding="utf-8")
        return where
    except OSError as exc:                                # pragma: no cover
        print(f"  (could not keep the output of {name}: {exc})")
        return None


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
