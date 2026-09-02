"""
test_history.py — Tests for the SQLite session store, search and config drift.

Covers the promise of the transcript layer: that a session recorded now can be
questioned later. Searching, filtering by device and date, replaying a session
command by command, and reporting what changed on a device since last time.

Includes an end-to-end run against a scripted telnet device through the real
API, so recording is proven through the same path the browser uses rather than
by calling the store directly.

    python test_history.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-history-"))
paths._data_dir_cache = _TEMP

from backend.configs import diff_snapshots                            # noqa: E402
from backend.session.transcript import TranscriptParser               # noqa: E402
from backend.store import SessionStore, _to_fts_query                 # noqa: E402

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


def fresh_store() -> tuple[SessionStore, Path]:
    """A store backed by its own empty directory."""
    directory = Path(tempfile.mkdtemp(prefix="shellmate-store-"))
    paths._data_dir_cache = directory
    instance = SessionStore()
    instance.connect()
    return instance, directory


def record(instance: SessionStore, session_id: str, metadata: dict, stream: str) -> None:
    """Record a whole session from a raw terminal stream."""
    instance.start_session(session_id, metadata)
    parser = TranscriptParser()
    for rec in parser.feed(stream):
        instance.add_command(session_id, rec)
    final = parser.flush()
    if final:
        instance.add_command(session_id, final)
    instance.end_session(session_id)


IOS_SESSION = (
    "glasgow-core#show ip interface brief\r\n"
    "Interface              IP-Address      OK? Status\r\n"
    "GigabitEthernet0/1     10.20.30.40     YES up\r\n"
    "GigabitEthernet0/2     unassigned      YES down\r\n"
    "glasgow-core#configure terminal\r\n"
    "glasgow-core(config)#interface GigabitEthernet0/2\r\n"
    "glasgow-core(config-if)#description uplink to edinburgh\r\n"
    "glasgow-core(config-if)#no shutdown\r\n"
    "glasgow-core(config-if)#"
)

JUNOS_SESSION = (
    "neteng@srx-edge> show chassis hardware\r\n"
    "Hardware inventory:\r\n"
    "Item             Version  Part number\r\n"
    "Chassis                            SRX345\r\n"
    "neteng@srx-edge> "
)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_recording_and_replay() -> None:
    print("\n-- Recording and replay --")
    instance, directory = fresh_store()
    try:
        record(instance, "s1", {
            "display_label": "glasgow-core", "hostname": "glasgow-core",
            "connection_type": "ssh", "username": "neteng",
            "target": "10.20.30.40:22",
        }, IOS_SESSION)

        session = instance.get_session("s1")
        check("session is retrievable", session is not None)
        if not session:
            return

        commands = [c["command"] for c in session["commands"]]
        check("every command recorded in order",
              commands == ["show ip interface brief", "configure terminal",
                           "interface GigabitEthernet0/2",
                           "description uplink to edinburgh", "no shutdown"],
              f"got {commands}")

        check("sequence numbers are contiguous",
              [c["sequence"] for c in session["commands"]] == [1, 2, 3, 4, 5],
              f"got {[c['sequence'] for c in session['commands']]}")

        first = session["commands"][0]
        check("output stored against the right command",
              "GigabitEthernet0/1" in first["output"], f"got {first['output']!r}")
        check("config-mode prompt recorded",
              session["commands"][4]["prompt"] == "glasgow-core(config-if)#",
              f"got {session['commands'][4]['prompt']!r}")
        check("session marked ended", session["ended_at"] is not None)
    finally:
        instance.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_search() -> None:
    print("\n-- Full-text search --")
    instance, directory = fresh_store()
    try:
        record(instance, "s1", {"hostname": "glasgow-core", "display_label": "glasgow-core"},
               IOS_SESSION)
        record(instance, "s2", {"hostname": "srx-edge", "display_label": "srx-edge"},
               JUNOS_SESSION)

        check("search engine is FTS5", instance.stats()["search"] == "fts5",
              f"got {instance.stats()['search']}")

        hits = instance.search("shutdown")
        check("finds a command by name",
              any(h["command"] == "no shutdown" for h in hits), f"got {hits}")

        hits = instance.search("edinburgh")
        check("finds text inside a command",
              any("edinburgh" in h["command"] for h in hits), f"got {hits}")

        # Punctuation is FTS5 syntax; unquoted it would be a query error.
        hits = instance.search("10.20.30.40")
        check("finds an IP address in output", len(hits) >= 1, f"got {hits}")

        hits = instance.search("GigabitEthernet0/2")
        check("finds an interface name with a slash", len(hits) >= 1, f"got {hits}")

        hits = instance.search("chassis")
        check("searches across sessions",
              any(h["hostname"] == "srx-edge" for h in hits), f"got {hits}")

        hits = instance.search("", hostname="srx-edge")
        check("filters by device",
              hits and all(h["hostname"] == "srx-edge" for h in hits), f"got {hits}")

        hits = instance.search("shutdown", hostname="srx-edge")
        check("device filter excludes other devices", hits == [], f"got {hits}")

        check("nonsense query returns nothing", instance.search("zzzznotpresent") == [])

        check("snippets accompany hits",
              all("snippet" in h for h in instance.search("shutdown")))
    finally:
        instance.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_date_filtering() -> None:
    print("\n-- Date filtering --")
    instance, directory = fresh_store()
    try:
        record(instance, "s1", {"hostname": "glasgow-core"}, IOS_SESSION)
        now = time.time()

        check("finds commands within the window",
              len(instance.search("shutdown", since=now - 3600, until=now + 3600)) >= 1)
        check("excludes commands before the window",
              instance.search("shutdown", since=now + 3600) == [])
        check("excludes commands after the window",
              instance.search("shutdown", until=now - 3600) == [])
    finally:
        instance.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_fts_query_escaping() -> None:
    print("\n-- FTS query building --")
    # The last word is a prefix, because the panel searches as you type and
    # every word is a partial word until the moment it is finished (#271).
    check("words are quoted and ANDed, the last one a prefix",
          _to_fts_query("show version") == '"show" AND "version"*',
          f"got {_to_fts_query('show version')}")
    check("punctuation is safely quoted",
          _to_fts_query("10.1.1.1") == '"10.1.1.1"*', f"got {_to_fts_query('10.1.1.1')}")
    # A quote in the input must not close the quoting we add, or the query
    # becomes a syntax error rather than a search.
    check("embedded quotes cannot break out",
          _to_fts_query('a"b') == '"a" AND "b"*',
          f"got {_to_fts_query(chr(97) + chr(34) + chr(98))}")
    check("empty input yields an empty match", _to_fts_query("   ") == '""')


def test_session_listing_and_deletion() -> None:
    print("\n-- Listing and deletion --")
    instance, directory = fresh_store()
    try:
        record(instance, "s1", {"hostname": "glasgow-core"}, IOS_SESSION)
        record(instance, "s2", {"hostname": "srx-edge"}, JUNOS_SESSION)

        sessions = instance.list_sessions()
        check("both sessions listed", len(sessions) == 2, f"got {len(sessions)}")
        check("command counts included",
              all("command_count" in s for s in sessions), f"got {sessions}")
        check("newest first",
              sessions[0]["started_at"] >= sessions[1]["started_at"])

        check("device list is populated",
              set(instance.known_hostnames()) == {"glasgow-core", "srx-edge"},
              f"got {instance.known_hostnames()}")

        check("stats count devices", instance.stats()["devices"] == 2,
              f"got {instance.stats()}")

        check("deletion reports success", instance.delete_session("s1") is True)
        check("session is gone", instance.get_session("s1") is None)
        check("its commands are gone too", instance.search("shutdown") == [],
              f"got {instance.search('shutdown')}")
        check("deleting again reports failure", instance.delete_session("s1") is False)
    finally:
        instance.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_large_output_truncated() -> None:
    print("\n-- Oversized output --")
    instance, directory = fresh_store()
    try:
        from backend.store import MAX_OUTPUT_CHARS

        instance.start_session("s1", {"hostname": "big-device"})
        parser = TranscriptParser()
        huge = "line of show tech output\r\n" * 40000
        for rec in parser.feed(f"big-device#show tech-support\r\n{huge}big-device#"):
            instance.add_command("s1", rec)

        session = instance.get_session("s1")
        stored = session["commands"][0]["output"]
        check("oversized output is capped",
              len(stored) <= MAX_OUTPUT_CHARS + 200, f"got {len(stored):,} chars")
        check("truncation is disclosed, not silent",
              "truncated" in stored, f"tail: {stored[-120:]!r}")
    finally:
        instance.close()
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Config snapshots and drift
# ---------------------------------------------------------------------------


CONFIG_V1 = "hostname glasgow-core\n!\ninterface Gi0/1\n ip address 10.1.1.1 255.255.255.0\n!\nend"
CONFIG_V2 = ("hostname glasgow-core\n!\ninterface Gi0/1\n ip address 10.1.1.1 255.255.255.0\n"
             " description uplink\n!\ninterface Gi0/2\n no shutdown\n!\nend")


def test_snapshots_and_diff() -> None:
    print("\n-- Config snapshots and diff --")
    instance, directory = fresh_store()
    try:
        first = instance.add_snapshot("glasgow-core", CONFIG_V1, "s1")
        check("first snapshot stored", first["stored"] is True)

        again = instance.add_snapshot("glasgow-core", CONFIG_V1, "s2")
        check("identical config is not stored twice", again["stored"] is False)
        check("and is reported as unchanged", again["unchanged"] is True)

        changed = instance.add_snapshot("glasgow-core", CONFIG_V2, "s3")
        check("a changed config is stored", changed["stored"] is True)

        snapshots = instance.list_snapshots("glasgow-core")
        check("two snapshots retained", len(snapshots) == 2, f"got {len(snapshots)}")
        check("listing omits config bodies to stay small",
              "content" not in snapshots[0], f"got {sorted(snapshots[0])}")

        old = instance.get_snapshot(snapshots[1]["id"])
        new = instance.get_snapshot(snapshots[0]["id"])
        comparison = diff_snapshots(old, new)

        # V2 adds " description uplink", "!", "interface Gi0/2", " no shutdown".
        check("added lines counted", comparison["added"] == 4,
              f"got {comparison['added']}: {comparison['diff']}")
        check("removed lines counted", comparison["removed"] == 0,
              f"got {comparison['removed']}")
        check("diff mentions the new interface",
              "interface Gi0/2" in comparison["diff"], f"got {comparison['diff']}")
        # The +++/--- header lines start with the same characters as real
        # changes and would inflate the count by one each if included.
        check("diff header lines are not counted as changes",
              comparison["changed"] == 4, f"got {comparison['changed']}")

        check("latest snapshot is the newest",
              instance.latest_snapshot("glasgow-core")["sha256"] == new["sha256"])
        check("unknown device has no snapshot",
              instance.latest_snapshot("never-seen") is None)
    finally:
        instance.close()
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_end_to_end_recording() -> None:
    """A session opened through the API must be recorded and searchable."""
    print("\n-- End to end through the API --")
    import backend.store as store_module
    from fastapi.testclient import TestClient
    from test_connections import FakeTelnetServer

    directory = Path(tempfile.mkdtemp(prefix="shellmate-e2e-"))
    paths._data_dir_cache = directory
    # The app holds a module-level store bound to the old directory.
    store_module.store = SessionStore()

    server = FakeTelnetServer(b"\r\nlab-switch> ", echo=True)
    try:
        import backend.app as app_module
        import backend.connections.manager as manager_module
        app_module.store = store_module.store
        manager_module.store = store_module.store

        with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
            created = client.post("/api/sessions", json={
                "connection_type": "telnet", "hostname": "127.0.0.1",
                "port": server.port, "display_label": "lab-switch",
            })
            check("session created", created.status_code == 200, created.text)
            if created.status_code != 200:
                return
            session_id = created.json()["session_id"]

            with client.websocket_connect(f"/ws/terminal/{session_id}") as ws:
                ws.receive_json()                       # banner
                ws.send_json({"type": "input", "data": "show version\r\n"})
                # Deliberately not draining the socket. Recording happens in
                # the server's read loop whether or not the client reads, and
                # receive_json() blocks forever once the expected messages run
                # out — which is exactly how this test first hung.
                deadline = time.time() + 10
                while time.time() < deadline:
                    if store_module.store.search("version"):
                        break
                    time.sleep(0.25)

            time.sleep(0.5)
            client.delete(f"/api/sessions/{session_id}")

            listed = client.get("/api/history/sessions").json()
            check("session appears in history",
                  any(s["id"] == session_id for s in listed), f"got {listed}")

            stats = client.get("/api/history/stats").json()
            check("history stats report the session", stats["sessions"] >= 1, f"got {stats}")

            found = client.get("/api/history/search", params={"q": "version"}).json()
            check("the command is searchable afterwards",
                  found["count"] >= 1, f"got {found}")

            detail = client.get(f"/api/history/sessions/{session_id}").json()
            check("session detail lists its commands",
                  any("version" in c["command"] for c in detail["commands"]),
                  f"got {[c['command'] for c in detail['commands']]}")

            check("deleting from history works",
                  client.delete(f"/api/history/sessions/{session_id}").status_code == 200)
            check("and it is then a 404",
                  client.get(f"/api/history/sessions/{session_id}").status_code == 404)
    finally:
        server.close()
        try:
            store_module.store.close()
        except Exception:
            pass
        shutil.rmtree(directory, ignore_errors=True)


def main() -> int:
    print("=" * 52)
    print("  Session history and config drift tests")
    print("=" * 52)

    for test in (
        test_recording_and_replay,
        test_search,
        test_date_filtering,
        test_fts_query_escaping,
        test_session_listing_and_deletion,
        test_large_output_truncated,
        test_snapshots_and_diff,
        test_end_to_end_recording,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_TEMP, ignore_errors=True)

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
