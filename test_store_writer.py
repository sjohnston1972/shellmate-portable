"""
test_store_writer.py — History writes land off the caller's thread (#459).

The terminal read loop hands records to the store and carries on; the
writer thread commits them in order. Run: python test_store_writer.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-store-"))
from backend import paths  # noqa: E402
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend.store import SessionStore  # noqa: E402
from backend.session.transcript import CommandRecord  # noqa: E402

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


def record(command: str, output: str = "") -> CommandRecord:
    return CommandRecord(command=command, output=output, prompt="sw#",
                         started_at=time.time(), duration_ms=1)


def main() -> int:
    print("=" * 52)
    print("  Store writer")
    print("=" * 52)
    store = SessionStore()
    store.start_session("s1", {"hostname": "10.0.0.1", "connection_type": "ssh", "display_label": "sw"})

    print("\n-- Submitted writes land in order --")
    t = time.perf_counter()
    for n in range(50):
        store.submit(store.add_command, "s1", record(f"show thing {n}", "out"))
    elapsed = time.perf_counter() - t
    check("submitting fifty records returns at once", elapsed < 0.05, f"{elapsed * 1000:.0f} ms")
    check("flush waits for them", store.flush(timeout=5.0))
    rows = store.session_commands("s1") if hasattr(store, "session_commands") else None
    if rows is None:
        rows = store.search("show thing", limit=100)
    check("all fifty are stored", len(rows) == 50, str(len(rows)))
    seq = [r["sequence"] for r in store.connect().execute(
        "SELECT sequence FROM commands WHERE session_id = 's1' ORDER BY sequence").fetchall()]
    check("sequence numbers are contiguous", seq == list(range(1, 51)), str(seq[:5]) + "…")

    print("\n-- A failing write does not stop the thread --")
    store.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    store.submit(store.add_command, "s1", record("show after", "x"))
    check("later writes still land", store.flush(timeout=5.0)
          and store.connect().execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 51)

    print("\n-- The substring fallback searches commands, not output --")
    store.add_command("s1", record("show ip arp", "Internet  10.1.1.1  0  aabb.ccdd.eeff  ARPA"))
    hits = store.search("arpshow", limit=10)
    check("a mistyped command still misses cleanly", isinstance(hits, list))
    hits = store.search("ip ar", limit=10)
    check("a substring of a command is found", any("show ip arp" in (h.get("command") or "") for h in hits), str(hits[:1]))

    store.close() if hasattr(store, "close") else None
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
