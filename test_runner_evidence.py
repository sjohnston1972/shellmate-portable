"""
test_runner_evidence.py — A failing test leaves evidence behind (#586).

There is an intermittent in this suite. It has now been three different
files — `test_sftp.py`, `test_ansible_env_keys.py`, `test_ansible_templates.py`
— on two machines, each passing immediately when run on its own. That rules
out both explanations reached for first: it is not one bad test, and it is
not the load of running them in parallel, because the runner has always been
sequential.

What it actually is remains unknown, and the reason it remained unknown is
that every occurrence arrived as one word. `failed: test_sftp.py`, with the
output long since scrolled past or thrown away by CI. A summary line is
enough for a test that fails every time and useless for one that fails now
and then.

The first failure this kept already sharpened the question. All three are
Playwright suites, and the one with a stack attached died on a five-second
`wait_for_selector` for a validation banner that a lone run paints
immediately. So the shape to look for next is a browser action that
occasionally takes seconds rather than milliseconds — not a wrong
assertion. That is a hypothesis, not a finding, and it is written here
rather than acted on because acting on it is what produced the two wrong
answers above.

So this asserts the thing that has to hold before the next occurrence is
worth anything: a file that fails leaves its output somewhere the runner
names, and a file that passes leaves nothing at all. It does not assert
what the intermittent is. It makes the next one answerable.

Run: python test_runner_evidence.py
"""

import atexit
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

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


def _runner():
    """The runner, imported rather than shelled out to, so its own
    functions can be asserted about rather than only its exit code."""
    spec = importlib.util.spec_from_file_location(
        "run_tests", ROOT / "tools" / "run_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: A probe that fails the way a real test fails: prints its checks, then
#: exits non-zero. Named so that if it is ever left behind by a crash, the
#: name says what it is and that it should not be there.
PROBE_FAIL = '''
print("  OK   something that worked")
print("  FAIL the check that did not")
print("       a distinctive string only this probe prints: WOMBAT")
import sys
sys.exit(1)
'''

PROBE_PASS = '''
print("  OK   nothing to see here")
'''


def main() -> int:
    runner = _runner()

    # This writes into a directory of its own, never the real one.
    #
    # The first version pointed at `.test-failures/` itself and cleared it
    # at both ends — and then, running later in the alphabet than the file
    # that had actually failed that night, deleted the only copy of the
    # evidence it exists to preserve. A test that tidies up a directory it
    # does not own is not tidying up.
    runner.EVIDENCE = Path(tempfile.mkdtemp(prefix="shellmate-evidence-"))
    shutil.rmtree(runner.EVIDENCE, ignore_errors=True)

    probe = ROOT / "test_zz_evidence_probe.py"
    # Belt and braces: a probe left in the repo root would fail every
    # future run of the suite, which is a worse outcome than this test
    # failing.
    atexit.register(lambda: probe.exists() and probe.unlink())

    try:
        print("\n-- A file that fails keeps its output --")
        probe.write_text(PROBE_FAIL, encoding="utf-8")
        code = runner.main(["zz_evidence_probe"])
        check("the runner reports the failure", code == 1, str(code))

        kept = sorted(runner.EVIDENCE.glob("test_zz_evidence_probe-*.log")) \
            if runner.EVIDENCE.exists() else []
        check("it kept a log named after the file that failed",
              len(kept) == 1, str(kept))
        if kept:
            text = kept[0].read_text(encoding="utf-8")
            check("the log carries the failing output itself",
                  "WOMBAT" in text,
                  "a log that does not carry the checks is a log that "
                  "answers nothing")
            check("and says when it happened and how it ended",
                  "when:" in text and "exit code: 1" in text, text[:200])
            check("and which interpreter ran it",
                  "python:" in text,
                  "two machines, two different failures — the version is "
                  "the first thing anybody compares")

        print("\n-- A file that passes keeps nothing --")
        shutil.rmtree(runner.EVIDENCE, ignore_errors=True)
        probe.write_text(PROBE_PASS, encoding="utf-8")
        code = runner.main(["zz_evidence_probe"])
        check("the runner reports success", code == 0, str(code))
        check("and wrote nothing at all",
              not runner.EVIDENCE.exists()
              or not list(runner.EVIDENCE.glob("*.log")),
              "a directory that fills up on good runs is one people delete")

        print("\n-- The output still streams as it goes --")
        # Captured here only to assert it was streamed; a suite that shows
        # nothing for ten minutes is a suite nobody runs locally.
        code, text = runner._run(probe, 60.0)
        check("the run returns what it printed",
              code == 0 and "nothing to see here" in text, repr(text[:120]))

    finally:
        if probe.exists():
            probe.unlink()
        shutil.rmtree(runner.EVIDENCE, ignore_errors=True)

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
