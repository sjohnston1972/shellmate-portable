"""
test_change_api.py — Opening and closing a change over the API (#544).

`test_change.py` covers where a change lives. This covers what happens
around it: the capture at each end, the diff, the commands gathered from
history, and the states that are easy to conflate.

The one worth the file is the last of those. **"Nothing changed" and "we
could not look" are different facts**, and a change record that renders the
second as the first tells a change board the work had no effect. Both ends
can fail independently — a device that answers at the start and not at the
end is the ordinary shape of a change that reloaded it — so the record
carries `comparable` and the reason, and the tests check all four
combinations.

Capture is stubbed. What is under test is how ShellMate brackets a piece of
work, not whether paramiko can open a second channel.

    python test_change_api.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-change-api-"))
paths._data_dir_cache = _TEMP

from fastapi.testclient import TestClient                   # noqa: E402

from backend import app as app_module                       # noqa: E402
from backend import change as change_module                 # noqa: E402
from backend import store as store_module                   # noqa: E402

passed = 0
failed: list[str] = []

client = TestClient(app_module.app, base_url="http://127.0.0.1")
store = store_module.store

BEFORE = "hostname core-sw-01\ninterface Gi1/0/1\n description old uplink\n"
AFTER = "hostname core-sw-01\ninterface Gi1/0/1\n description new uplink\n"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def register(session_id: str, hostname: str = "core-sw-01") -> dict:
    """Put a session in the manager without connecting to anything."""
    session = {
        "session_id": session_id,
        "hostname": hostname,
        "display_label": hostname,
        "username": "steven",
        "target": "10.0.0.1:22",
        "connection_type": "ssh",
        "alerts": SimpleNamespace(payload=lambda: {"pending": None}),
    }
    app_module.session_manager._sessions[session_id] = session
    return session


def stub_capture(*texts, fail_on=()):
    """
    Replace capture_config with one that returns the given texts in turn.

    `fail_on` names call indexes that raise instead, which is how a device
    that answers at the start and not at the end is expressed.
    """
    calls = {"n": 0}

    def capture(session):
        index = calls["n"]
        calls["n"] += 1
        if index in fail_on:
            raise RuntimeError("The session is no longer connected.")
        text = texts[min(index, len(texts) - 1)]
        return store.add_snapshot(session.get("hostname", ""), text,
                                  session.get("session_id", ""))

    app_module.capture_config = capture
    return calls


def reset() -> None:
    path = change_module._file()
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------

def test_a_whole_change() -> None:
    print("\n-- Start, work, end --")
    reset()
    register("s1")
    stub_capture(BEFORE, AFTER)

    res = client.post("/api/sessions/s1/change/start",
                      json={"note": "Retitling the uplink", "ticket": "NET-1"})
    check("starting one succeeds", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:160]}")
    started = res.json()["change"]
    check("the record carries the note",
          started["note"] == "Retitling the uplink", str(started))
    check("and a baseline was captured", started["before_id"] is not None)

    res = client.get("/api/changes")
    check("it is listed as open",
          [c["hostname"] for c in res.json()["changes"]] == ["core-sw-01"],
          res.text[:160])

    res = client.post("/api/sessions/s1/change/end")
    check("ending it succeeds", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:160]}")
    record = res.json()

    check("the diff names what moved",
          "new uplink" in record["diff"] and "old uplink" in record["diff"],
          record["diff"][:200])
    check("the counts are there",
          record["added"] == 1 and record["removed"] == 1, str(record))
    check("both snapshot ids are reported",
          record["old_id"] and record["new_id"], str(record))
    check("it says the two ends are comparable", record["comparable"] is True)
    check("the window is reported in seconds, not rounded to a day",
          "window_seconds" in record and record["window_seconds"] >= 0,
          str(record.get("window_seconds")))
    check("and the change is closed afterwards",
          client.get("/api/changes").json()["changes"] == [])


def test_the_commands_typed_in_the_window() -> None:
    """
    Gathered from history, not tracked on the session.

    Same reason the record is keyed on the hostname: by the time a change
    ends there may have been three sessions, or a different one.
    """
    print("\n-- What was typed --")
    reset()
    register("s2")
    stub_capture(BEFORE, AFTER)

    # A command from before the window opens, which must not be swept in.
    store.start_session("older", {"hostname": "core-sw-01", "label": "core-sw-01",
                                  "connection_type": "ssh"})
    store.add_command("older", SimpleNamespace(
        command="show version", output="Cisco IOS", prompt="core-sw-01#",
        started_at=time.time() - 3600, duration_ms=10))
    store.flush()

    client.post("/api/sessions/s2/change/start", json={"note": "Window"})
    time.sleep(0.05)

    store.start_session("inside", {"hostname": "core-sw-01", "label": "core-sw-01",
                                   "connection_type": "ssh"})
    for command in ("conf t", "interface Gi1/0/1", "description new uplink"):
        store.add_command("inside", SimpleNamespace(
            command=command, output="", prompt="core-sw-01(config)#",
            started_at=time.time(), duration_ms=5))
    store.flush()

    record = client.post("/api/sessions/s2/change/end").json()
    typed = [c["command"] for c in record["commands"]]

    check("the commands from inside the window are there",
          "interface Gi1/0/1" in typed and "description new uplink" in typed,
          str(typed))
    check("the one from before it is not",
          "show version" not in typed,
          "a change record must not claim commands from before it opened")
    check("they are in the order they were run",
          typed == sorted(typed, key=lambda c: typed.index(c)), str(typed))

    store.delete_session("older")
    store.delete_session("inside")


def test_the_two_kinds_of_nothing() -> None:
    """
    Four combinations, because both ends fail independently.

    A device that answers at the start and not at the end is the ordinary
    shape of a change that reloaded it — and "no difference" reported when
    the truth is "we could not look" tells a change board the work had no
    effect.
    """
    print("\n-- Nothing changed, or nothing seen --")

    # Both ends captured, nothing moved.
    reset()
    register("s3")
    stub_capture(BEFORE, BEFORE)
    client.post("/api/sessions/s3/change/start", json={})
    record = client.post("/api/sessions/s3/change/end").json()
    check("identical captures are comparable and empty",
          record["comparable"] is True and record["changed"] == 0,
          str({k: record[k] for k in ("comparable", "changed")}))

    # Captured at the start, gone at the end. The reload case.
    reset()
    register("s4")
    stub_capture(BEFORE, AFTER, fail_on=(1,))
    client.post("/api/sessions/s4/change/start", json={})
    record = client.post("/api/sessions/s4/change/end").json()
    check("a device that goes away at the end is not comparable",
          record["comparable"] is False,
          "this would otherwise read as a change that did nothing")
    check("and the record says why",
          "no longer connected" in record["capture_error"],
          record["capture_error"])
    check("the baseline is still reported",
          record["old_id"] is not None,
          "the capture at the start is evidence even with no closing one")

    # Nothing captured at the start. The window still opens.
    reset()
    register("s5")
    stub_capture(AFTER, fail_on=(0,))
    res = client.post("/api/sessions/s5/change/start", json={"note": "Odd box"})
    check("a device that will not be captured still gets a window",
          res.status_code == 200, f"HTTP {res.status_code}: {res.text[:140]}")
    check("and the record says there is no baseline",
          res.json()["change"]["before_id"] is None
          and res.json()["change"]["capture_error"] != "",
          str(res.json()["change"]))
    record = client.post("/api/sessions/s5/change/end").json()
    check("ending it is not comparable either", record["comparable"] is False)


def test_the_states_that_are_not_errors_and_the_ones_that_are() -> None:
    print("\n-- Refusals --")
    reset()
    register("s6")
    stub_capture(BEFORE, AFTER)

    res = client.post("/api/sessions/s6/change/end")
    check("ending a change nobody started is a 404",
          res.status_code == 404, f"HTTP {res.status_code}")
    check("and it names the device",
          "core-sw-01" in res.json().get("detail", ""), res.text[:140])

    client.post("/api/sessions/s6/change/start", json={"note": "First"})
    res = client.post("/api/sessions/s6/change/start", json={"note": "Second"})
    check("a second change on the same device is a 409, not a 400",
          res.status_code == 409,
          f"HTTP {res.status_code} — the request is fine, the state is not")
    check("and the refusal says what is in the way",
          "First" in res.json().get("detail", ""), res.text[:160])

    res = client.post("/api/sessions/s6/change/start",
                      json={"note": "x", "tikcet": "typo"})
    check("a misspelled field is a 422, not a silent 200",
          res.status_code == 422, f"HTTP {res.status_code}")

    res = client.post("/api/sessions/nope/change/start", json={})
    check("an unknown session is a 404", res.status_code == 404,
          f"HTTP {res.status_code}")

    # Abandon leaves nothing behind.
    res = client.post("/api/sessions/s6/change/abandon")
    check("abandoning reports it", res.json()["abandoned"] is True)
    check("and nothing is open", client.get("/api/changes").json()["changes"] == [])


def test_a_session_with_no_device_name() -> None:
    """
    A change needs something to be about, and the session id is not it.

    A tab that has not yet seen a prompt has no hostname, and a change keyed
    on nothing could never be found again from anywhere else.
    """
    print("\n-- Nothing to be about --")
    reset()
    session = register("s7", hostname="")
    session["display_label"] = ""
    session["target"] = ""
    stub_capture(BEFORE)

    res = client.post("/api/sessions/s7/change/start", json={})
    check("it is refused with an explanation", res.status_code == 400,
          f"HTTP {res.status_code}")
    check("that says what to do about it",
          "rename the tab" in res.json().get("detail", "").lower(),
          res.text[:200])


def test_a_pending_reload_is_carried() -> None:
    """
    A record that omits the reload describes a state the device is leaving.
    """
    print("\n-- Still hanging over it --")
    reset()
    session = register("s8")
    session["alerts"] = SimpleNamespace(payload=lambda: {
        "pending": {"kind": "reload", "seconds_left": 540, "source": "typed"}})
    stub_capture(BEFORE, AFTER)

    client.post("/api/sessions/s8/change/start", json={})
    record = client.post("/api/sessions/s8/change/end").json()
    check("the pending reload is in the record",
          (record.get("pending") or {}).get("kind") == "reload",
          str(record.get("pending")))

    # And a tracker that throws must not take the record with it.
    reset()
    session2 = register("s9")

    def boom():
        raise RuntimeError("tracker is confused")

    session2["alerts"] = SimpleNamespace(payload=boom)
    stub_capture(BEFORE, AFTER)
    client.post("/api/sessions/s9/change/start", json={})
    res = client.post("/api/sessions/s9/change/end")
    check("a broken tracker does not lose the whole record",
          res.status_code == 200 and res.json()["pending"] is None,
          f"HTTP {res.status_code}")


def fake_estate(answers):
    """
    Four injected callables over a dict of {hostname: config text or None}.

    None means the capture raises — a device that answers the connection
    and then will not give up its configuration, which is the shape the
    single-device path already has a rule for.
    """
    opened, destroyed = [], []

    def open_session_for(profile):
        # Nothing has a live session: every device goes down the headless
        # path, which is the one a scheduled window would actually use.
        return None

    def connect(profile):
        host = profile.get("hostname") or profile.get("name")
        if host not in answers:
            raise RuntimeError(f"could not reach {host}")
        opened.append(host)
        return {"session_id": f"h-{host}", "hostname": host}

    def capture(session):
        text = answers.get(session["hostname"])
        if text is None:
            raise RuntimeError("The device did not answer show run.")
        return store.add_snapshot(session["hostname"], text,
                                  session["session_id"])

    def destroy(session):
        destroyed.append(session["hostname"])

    return connect, capture, open_session_for, destroy, opened, destroyed


def profile(name, hostname, **extra):
    return {"id": f"p-{name}", "name": name, "hostname": hostname,
            "connection_type": "ssh", "has_saved_credentials": True, **extra}


def test_a_change_across_a_group() -> None:
    print("\n-- A whole site --")
    reset()

    profiles = [profile("sw1", "site-sw-01"), profile("sw2", "site-sw-02"),
                profile("sw3", "site-sw-03")]
    connect, capture, opener, destroy, opened, destroyed = fake_estate({
        "site-sw-01": BEFORE, "site-sw-02": BEFORE, "site-sw-03": BEFORE,
    })

    out = change_module.start_group(
        "glasgow/core", profiles, connect, capture, opener, destroy,
        note="Quarterly AAA rollout", ticket="NET-9")

    check("every member got a window", len(out["started"]) == 3, str(out))
    check("nothing was skipped or failed",
          not out["skipped"] and not out["failed"], str(out))
    check("each carries a baseline",
          all(s["before_id"] for s in out["started"]), str(out["started"]))
    check("headless sessions are cleaned up",
          sorted(destroyed) == sorted(opened),
          f"opened {opened}, destroyed {destroyed}")
    check("and all three are open",
          len(change_module.open_changes()) == 3)

    # Work happens; two of the three actually change.
    connect, capture, opener, destroy, _, destroyed2 = fake_estate({
        "site-sw-01": AFTER, "site-sw-02": AFTER, "site-sw-03": BEFORE,
    })
    out = change_module.end_group("glasgow/core", profiles, connect, capture,
                                  opener, destroy)

    check("one record per device, not one merged diff",
          len(out["records"]) == 3,
          "eight switches' hunks in one diff loses whose line is whose")
    check("the two that changed are named",
          sorted(out["changed"]) == ["site-sw-01", "site-sw-02"],
          str(out["changed"]))
    check("the one that did not is still a record",
          any(r["hostname"] == "site-sw-03" and r["changed"] == 0
              for r in out["records"]), str(out["changed"]))
    check("every record is comparable",
          all(r["comparable"] for r in out["records"]))
    check("and no windows are left open",
          change_module.open_changes() == [])


def test_a_group_where_not_everything_answers() -> None:
    """
    Partial success is the normal outcome of a maintenance window.

    Six of eight switches answering is six bracketed changes, not a
    failure — and the two that did not have to be named, because a group
    result that quietly covers three of five devices is worse than one
    that covers none.
    """
    print("\n-- Not everything answers --")
    reset()

    profiles = [
        profile("sw1", "mixed-sw-01"),
        profile("sw2", "mixed-sw-02"),
        profile("serial", "mixed-con-03", connection_type="serial"),
        profile("nocreds", "mixed-sw-04", has_saved_credentials=False),
        # A profile with a name but no hostname is keyed on the name — the
        # same fallback the single-session path uses, and reachable here
        # because the fake estate answers to it.
        profile("named-only", ""),
        # Neither. There is nothing to key a change on at all.
        {"id": "p-blank", "name": "", "hostname": "",
         "connection_type": "ssh", "has_saved_credentials": True},
    ]
    # sw-02 connects but will not give up its configuration; sw-05 is not
    # reachable at all.
    connect, capture, opener, destroy, _, _ = fake_estate({
        "mixed-sw-01": BEFORE, "mixed-sw-02": None, "named-only": BEFORE,
    })

    out = change_module.start_group("mixed", profiles, connect, capture,
                                    opener, destroy, note="Partial")

    started = {s["name"] for s in out["started"]}
    skipped = {s["name"]: s["why"] for s in out["skipped"]}

    check("the device that answered is bracketed", "sw1" in started, str(started))
    check("so is the one that would not be captured",
          "sw2" in started,
          "the device that will not answer show run is the one most worth "
          "bracketing")
    check("and it carries the reason there is no baseline",
          any(s["name"] == "sw2" and s["capture_error"] for s in out["started"]),
          str(out["started"]))

    check("a serial console is skipped, with the reason",
          "not an SSH connection" in skipped.get("serial", ""), str(skipped))
    check("a profile with no saved credentials is skipped",
          "no saved credentials" in skipped.get("nocreds", ""), str(skipped))
    check("a profile with a name but no hostname is keyed on the name",
          "named-only" in started,
          "the same fallback the single-session path uses")
    check("a profile with neither is skipped rather than keyed on ''",
          any("no device name" in s for s in skipped.values()), str(skipped))
    check("nothing here was a failure — every case had a reason",
          out["failed"] == [], str(out["failed"]))

    for host in ("mixed-sw-01", "mixed-sw-02", "named-only"):
        change_module.end(host)


def test_a_device_that_cannot_be_reached_is_a_failure() -> None:
    """
    Distinct from a skip. A skip has a reason ShellMate can state up front —
    it is a serial console, it has no credentials. A failure is a device
    that should have answered and did not, and it is the one somebody has
    to go and look at.
    """
    print("\n-- Unreachable --")
    reset()

    profiles = [profile("here", "reach-sw-01"), profile("gone", "reach-sw-99")]
    connect, capture, opener, destroy, _, _ = fake_estate({"reach-sw-01": BEFORE})
    out = change_module.start_group("reach", profiles, connect, capture,
                                    opener, destroy, note="Reachability")

    check("the reachable one is bracketed",
          [s["name"] for s in out["started"]] == ["here"], str(out["started"]))
    check("the unreachable one is a failure, not a skip",
          [f["name"] for f in out["failed"]] == ["gone"], str(out))
    check("and the reason is carried",
          "could not reach" in out["failed"][0]["why"], str(out["failed"]))
    check("no window was opened for it",
          change_module.active("reach-sw-99") is None,
          "a failed connection must not leave a record claiming a baseline")

    change_module.end("reach-sw-01")


def test_a_member_already_inside_a_change() -> None:
    """
    Skipped, and certainly not overwritten.

    Somebody may have opened a window on one switch by hand an hour ago.
    Taking a second baseline would spend theirs, and their capture is
    evidence.
    """
    print("\n-- One member is already inside one --")
    reset()

    change_module.start("shared-sw-01", note="Opened by hand", before_id=99)
    profiles = [profile("sw1", "shared-sw-01"), profile("sw2", "shared-sw-02")]
    connect, capture, opener, destroy, _, _ = fake_estate({
        "shared-sw-01": BEFORE, "shared-sw-02": BEFORE,
    })

    out = change_module.start_group("shared", profiles, connect, capture,
                                    opener, destroy, note="Group window")

    check("the one already inside a change is skipped",
          any(s["name"] == "sw1" and "already open" in s["why"]
              for s in out["skipped"]), str(out["skipped"]))
    check("the other one still gets a window",
          [s["name"] for s in out["started"]] == ["sw2"], str(out["started"]))

    kept = change_module.active("shared-sw-01")
    check("the hand-opened change is untouched",
          kept is not None and kept.note == "Opened by hand"
          and kept.before_id == 99,
          "taking a second baseline would spend the one that is evidence")

    change_module.end("shared-sw-01")
    change_module.end("shared-sw-02")


def test_ending_a_group_only_touches_open_windows() -> None:
    """
    A group whose membership moved mid-window.

    Somebody adds a switch in the afternoon; it never had a change opened
    on it. Reporting that as a failed close would be inventing a problem.
    """
    print("\n-- Membership moved --")
    reset()

    profiles = [profile("sw1", "moved-sw-01")]
    connect, capture, opener, destroy, _, _ = fake_estate({"moved-sw-01": BEFORE})
    change_module.start_group("moved", profiles, connect, capture, opener,
                              destroy, note="Started with one")

    profiles.append(profile("sw2", "moved-sw-02"))
    connect, capture, opener, destroy, _, _ = fake_estate({
        "moved-sw-01": AFTER, "moved-sw-02": AFTER,
    })
    out = change_module.end_group("moved", profiles, connect, capture,
                                  opener, destroy)

    check("only the device with a window gets a record",
          [r["hostname"] for r in out["records"]] == ["moved-sw-01"],
          str([r["hostname"] for r in out["records"]]))
    check("the newcomer is skipped, not failed",
          any(s["name"] == "sw2" and "no change was open" in s["why"]
              for s in out["skipped"]), str(out["skipped"]))
    check("and nothing is reported as a failure", out["failed"] == [])


def test_a_device_that_goes_away_before_the_group_ends() -> None:
    """The reload case, at group scale."""
    print("\n-- Gone by the end --")
    reset()

    profiles = [profile("sw1", "gone-sw-01"), profile("sw2", "gone-sw-02")]
    connect, capture, opener, destroy, _, _ = fake_estate({
        "gone-sw-01": BEFORE, "gone-sw-02": BEFORE,
    })
    change_module.start_group("gone", profiles, connect, capture, opener,
                              destroy, note="Firmware")

    # sw-02 is mid-reload and cannot be reached at all.
    connect, capture, opener, destroy, _, _ = fake_estate({"gone-sw-01": AFTER})
    out = change_module.end_group("gone", profiles, connect, capture,
                                  opener, destroy)

    check("both still produce a record", len(out["records"]) == 2, str(out))
    check("the unreachable one is listed as unmeasurable",
          out["unmeasurable"] == ["gone-sw-02"], str(out["unmeasurable"]))
    check("and it is not counted as changed",
          "gone-sw-02" not in out["changed"],
          "an unmeasurable change must not be reported as one that did "
          "nothing, nor as one that did something")
    check("its baseline is still in the record",
          any(r["hostname"] == "gone-sw-02" and r["old_id"]
              for r in out["records"]), str(out["records"]))


def test_the_group_routes() -> None:
    print("\n-- Over the API --")
    reset()

    res = client.post("/api/groups/nothing-here/change/start", json={})
    check("a group with no connections is a 404",
          res.status_code == 404, f"HTTP {res.status_code}")

    res = client.post("/api/groups/x/change/start",
                      json={"note": "n", "tikcet": "typo"})
    check("a misspelled field is a 422 before anything is touched",
          res.status_code == 422, f"HTTP {res.status_code}")


def main() -> int:
    print("=" * 52)
    print("  Change records over the API")
    print("=" * 52)

    real_capture = app_module.capture_config
    for test in (
        test_a_whole_change,
        test_the_commands_typed_in_the_window,
        test_the_two_kinds_of_nothing,
        test_the_states_that_are_not_errors_and_the_ones_that_are,
        test_a_session_with_no_device_name,
        test_a_pending_reload_is_carried,
        test_a_change_across_a_group,
        test_a_group_where_not_everything_answers,
        test_a_device_that_cannot_be_reached_is_a_failure,
        test_a_member_already_inside_a_change,
        test_ending_a_group_only_touches_open_windows,
        test_a_device_that_goes_away_before_the_group_ends,
        test_the_group_routes,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
    app_module.capture_config = real_capture

    store.close()
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
