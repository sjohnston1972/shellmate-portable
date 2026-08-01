"""
test_clear_history.py — Scoped deletion of recorded history.

Rows are inserted straight into SQLite rather than through add_command(),
because what is under test is which rows clear_history() selects, and the
recording path has its own gates (history.record, output capture) that would
decide whether the fixture exists at all.

Run: python test_clear_history.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

# A throwaway data dir, so a test run never touches the real history database.
#
# Set by assigning the cache directly, which is what the other tests do. There
# is no SHELLMATE_DATA_DIR environment variable — writing this test as though
# there were pointed it at the real ShellMate-Data and its seed() deleted the
# recorded history. Hence the assertion below: this file's fixtures begin by
# emptying every table, so a silent failure to redirect is destructive rather
# than merely wrong.
_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-clear-"))
paths._data_dir_cache = _TEMP

assert paths.data_dir() == _TEMP, (
    f"refusing to run: history would be written to {paths.data_dir()}")

from backend.store import SessionStore  # noqa: E402

passed = 0
failed = 0

NOW = time.time()
DAY = 86400.0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [ok]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  --  {detail}" if detail else ""))


def seed() -> SessionStore:
    """
    Two devices, each with one old session and one recent one, plus a
    snapshot apiece.
    """
    store = SessionStore()
    with store._lock:
        connection = store.connect()
        connection.execute("DELETE FROM commands")
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM config_snapshots")
        connection.execute("DELETE FROM config_baselines")

        rows = [
            ("s-old-1", "sw01", NOW - 30 * DAY),
            ("s-old-2", "sw02", NOW - 30 * DAY),
            ("s-new-1", "sw01", NOW - 1 * DAY),
            ("s-new-2", "sw02", NOW - 1 * DAY),
        ]
        for session_id, hostname, when in rows:
            connection.execute(
                """INSERT INTO sessions
                       (id, label, hostname, connection_type, username, target, started_at)
                   VALUES (?, ?, ?, 'ssh', 'u', ?, ?)""",
                (session_id, hostname, hostname, hostname, when),
            )
            connection.execute(
                """INSERT INTO commands
                       (session_id, sequence, command, output, prompt, ran_at)
                   VALUES (?, 1, 'show version', 'output', ?, ?)""",
                (session_id, hostname + "#", when),
            )

        for hostname, session_id in (("sw01", "s-old-1"), ("sw02", "s-old-2")):
            connection.execute(
                """INSERT INTO config_snapshots
                       (hostname, session_id, captured_at, content, sha256, line_count)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (hostname, session_id, NOW - 30 * DAY, f"hostname {hostname}\n", hostname),
            )
        connection.commit()
    return store


def counts(store: SessionStore) -> tuple[int, int, int]:
    with store._lock:
        connection = store.connect()
        return (
            connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0],
        )


print("\nClearing one device")
store = seed()
removed = store.clear_history(hostname="sw01")
check("counts what it removed",
      removed["commands"] == 2 and removed["sessions"] == 2, str(removed))
check("its snapshot goes with it", removed["snapshots"] == 1, str(removed))
with store._lock:
    left = sorted({r[0] for r in store.connect().execute("SELECT hostname FROM sessions")})
check("the other device is untouched", left == ["sw02"], str(left))
check("and its snapshot is untouched", counts(store)[2] == 1, str(counts(store)))

print("\nClearing by date keeps what the panel is showing")
store = seed()
removed = store.clear_history(before=NOW - 7 * DAY)
check("only the old commands go", removed["commands"] == 2, str(removed))
check("recent commands survive", counts(store)[0] == 2, str(counts(store)))
check("their sessions survive", counts(store)[1] == 2, str(counts(store)))
with store._lock:
    kept = sorted(r[0] for r in store.connect().execute("SELECT id FROM sessions"))
check("the surviving sessions are the recent ones",
      kept == ["s-new-1", "s-new-2"], str(kept))

print("\nA recent empty session is not swept up by a date-scoped clear")
# The bug this guards: a session that recorded nothing is empty from its first
# second, so deleting "sessions with no commands left" would take one opened a
# minute ago. The date bound is repeated on the session delete for this.
store = seed()
with store._lock:
    store.connect().execute(
        """INSERT INTO sessions
               (id, label, hostname, connection_type, username, target, started_at)
           VALUES ('s-empty', 'sw03', 'sw03', 'ssh', 'u', 'sw03', ?)""",
        (NOW - 60,))
    store.connect().commit()
store.clear_history(before=NOW - 7 * DAY)
with store._lock:
    still = store.connect().execute(
        "SELECT COUNT(*) FROM sessions WHERE id = 's-empty'").fetchone()[0]
check("the empty session stays", still == 1)

print("\nSnapshots can be kept")
store = seed()
removed = store.clear_history(hostname="sw01", include_snapshots=False)
check("none are counted", removed["snapshots"] == 0, str(removed))
check("both are still there", counts(store)[2] == 2, str(counts(store)))

print("\nBaselines never point at a snapshot that has gone")
store = seed()
with store._lock:
    snapshot_id = store.connect().execute(
        "SELECT id FROM config_snapshots WHERE hostname = 'sw01'").fetchone()[0]
store.set_baseline("sw01", snapshot_id)
store.clear_history(hostname="sw01")
with store._lock:
    orphans = store.connect().execute(
        """SELECT COUNT(*) FROM config_baselines
            WHERE snapshot_id NOT IN (SELECT id FROM config_snapshots)"""
    ).fetchone()[0]
check("no orphaned baseline", orphans == 0, f"{orphans} orphaned")

print("\nA pinned baseline is not exempt from an explicit clear")
# It is exempt from *ageing out*, because being old is what makes it a
# baseline. That is not the same as surviving somebody asking for the device's
# history to go.
check("its snapshot went too", counts(store)[2] == 1, str(counts(store)))

print("\nClearing everything")
store = seed()
store.clear_history()
check("nothing left", counts(store) == (0, 0, 0), str(counts(store)))

print("\nA scope that matches nothing is not an error")
store = seed()
removed = store.clear_history(hostname="no-such-device")
check("nothing removed",
      removed == {"commands": 0, "sessions": 0, "snapshots": 0}, str(removed))
check("everything survives", counts(store) == (4, 4, 2), str(counts(store)))

print("\n" + "=" * 52)
print(f"  {passed} passed  |  {failed} failed")
print("=" * 52)

sys.exit(1 if failed else 0)
