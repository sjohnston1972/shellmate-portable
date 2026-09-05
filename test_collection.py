"""
test_collection.py — Show commands on the backup timer (#547).

Configuration drift is one kind of drift; "which interfaces started
erroring this week" is the other. The scheduler is already logged into every
device in a group overnight, and the marginal cost of three more questions
while it is there is seconds.

Four properties, and the first is not negotiable:

**Read-only, checked twice.** A snippet marked `writes` cannot be scheduled,
a command on the platform's dangerous list cannot be scheduled, and both are
re-checked at the moment of running — the group file is one people are told
they may edit, and a scheduled overnight job is the worst possible place for
a command that changes something.

**Stored through the one store.** A synthetic session per device per run,
`connection_type = "collection"`, so History can tell it from what a person
typed and a filter can find "every collection from last night".

**Compared by command, not position.** A snippet edited between runs shifts
positions, and `show ip route` against last night's `show interfaces` is a
diff that is all noise and looks like all signal.

**Bounded.** Output per command by `history.max_output_chars`; runs per
device by `history.collection_keep`. Unbounded growth was the stated risk.

    python test_collection.py
"""

import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-collect-"))
paths._data_dir_cache = _TEMP

from backend import advanced, collection, scheduler, snippets   # noqa: E402
from backend.connections.ssh_handler import SSHHandler         # noqa: E402
from backend.store import store                                # noqa: E402

passed = 0
failed: list[str] = []

# The settle time is what makes a real capture wait for a stuttering link.
# Against a fake that answers instantly it is dead time, three times per
# command; the floor is what the registry allows, not zero.
advanced.update({"capture.idle_settle": 0.2})

ROOT = Path(__file__).parent
SCHED = (ROOT / "backend" / "scheduler.py").read_text(encoding="utf-8")
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
GROUPS_JS = (ROOT / "frontend" / "js" / "groups.js").read_text(encoding="utf-8")
HISTORY_JS = (ROOT / "frontend" / "js" / "history.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# --- a fake device on a fake second channel --------------------------------

class FakeChannel:
    """Answers each command from a script, like a device would."""

    # `_read_until_idle` has no end marker: it reads until the channel raises
    # `socket.timeout` and has been quiet for the settle time. So the fake
    # hands over each reply in one piece and then times out, which is what
    # a real channel does once the prompt has been printed. It opens with a
    # banner, because a read that gets nothing at all waits out the whole
    # timeout rather than settling.

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.sent: list[str] = []
        self._pending = b"sw1 login banner\nsw1#"
        self.closed = False

    def send(self, data: bytes) -> None:
        line = data.decode().strip()
        self.sent.append(line)
        reply = self.answers.get(line, f"% Invalid input: {line}")
        self._pending = f"{line}\n{reply}\nsw1#".encode()

    def recv(self, n: int) -> bytes:
        import socket
        if not self._pending:
            raise socket.timeout()
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self) -> None:
        self.closed = True


class FakeHandler(SSHHandler):
    """Enough of an SSH handler to open a second channel."""

    def __init__(self, channel):          # no super(): there is no socket
        self._channel = channel

    @property
    def is_connected(self) -> bool:
        return True

    def open_secondary_channel(self):
        return self._channel


def session_for(hostname: str, channel) -> dict:
    # `fingerprint` is what session_platform() reads; it is what the
    # onboarding step stores once the device has identified itself.
    return {"session_id": f"live-{hostname}", "hostname": hostname,
            "display_label": hostname, "handler": FakeHandler(channel),
            "username": "neteng",
            "fingerprint": {"platform": "ios", "confidence": 1.0}}


def make_snippet(name: str, commands: list[str], **fields):
    return snippets.save_snippet({"name": name, "commands": commands, **fields})


# ---------------------------------------------------------------------------

def test_what_may_be_scheduled() -> None:
    print("\n-- Read-only, checked twice --")

    ro = make_snippet("ints", ["show interfaces status"])
    rw = make_snippet("save", ["write memory"], writes=True)
    # Built directly: the library refuses to *save* an empty snippet, but a
    # hand-edited snippets.json can still hold one, and that is the route
    # eligible() has to be ready for.
    empty = snippets.Snippet(id="user-empty", name="nothing")
    hot = make_snippet("bounce", ["reload"], platform="ios")

    check("a read-only snippet is eligible", collection.eligible(ro) == "")
    check("one marked writes is not, and says so",
          "changing the device" in collection.eligible(rw))
    check("one with no commands is not",
          "no commands" in collection.eligible(empty))
    check("one on the platform's dangerous list is not, naming the command",
          "reload" in collection.eligible(hot),
          collection.eligible(hot))

    listed = {s["id"]: s for s in collection.eligible_snippets()}
    check("the saved ones are all listed, with reasons on those that cannot run",
          all(i in listed for i in (ro.id, rw.id, hot.id))
          and listed[rw.id]["reason"] and listed[hot.id]["reason"]
          and not listed[ro.id]["reason"],
          "a list that silently omitted the write snippets would have "
          "somebody wondering where theirs went")

    kept = collection.normalise([ro.id, rw.id, hot.id, "nope", ro.id])
    check("the schedule keeps only what may run, once each",
          kept == [ro.id], str(kept))

    plan = scheduler.normalise({"enabled": True, "every": "daily",
                                "at": "02:00", "collect": [ro.id, rw.id]})
    check("and the scheduler's own normalise applies the same rule",
          plan.get("collect") == [ro.id], str(plan),)
    check("through the group file too, not just the dialog",
          "collection.normalise" in SCHED,
          "the group file is one people are told they may edit")


def test_a_run_lands_in_history() -> None:
    print("\n-- Stored through the one store --")
    channel = FakeChannel({
        "terminal length 0": "",
        "show interfaces status": "Gi1/0/1  connected  1  a-full a-1000\nGi1/0/2  notconnect",
        "show ip arp": "10.0.0.1  0  aabb.cc00.0100  ARPA  Vlan1",
    })
    ro = make_snippet("ports", ["show interfaces status", "show ip arp"])
    rw = make_snippet("save2", ["write memory"], writes=True)

    out = collection.collect(session_for("sw1", channel), [ro.id, rw.id], "20260905-0200")
    check("two commands stored", out["stored"] == 2, str(out))
    check("the write snippet did not run even though it was passed in",
          "write memory" not in channel.sent, str(channel.sent),)
    check("paging was turned off first",
          channel.sent and channel.sent[0] == "terminal length 0", str(channel.sent))
    check("and the channel was closed", channel.closed)

    saved = store.get_session(out["session_id"])
    check("it is a collection session",
          saved and saved["connection_type"] == collection.KIND, str(saved and saved["connection_type"]))
    check("on the right device", saved["hostname"] == "sw1")
    check("labelled with the run", "20260905-0200" in saved["label"])
    check("the output is there, without the echo",
          saved["commands"][0]["output"].startswith("Gi1/0/1"),
          saved["commands"][0]["output"][:60])

    hits = store.search("notconnect", kind="collection")
    check("History finds it under the collection filter", len(hits) == 1, str(hits))
    check("and not under what I typed",
          store.search("notconnect", kind="live") == [])
    check("the run is listed for the device",
          any(r["id"] == out["session_id"] for r in collection.runs_for("sw1")))


def test_compare_with_the_previous_run() -> None:
    print("\n-- Compared by command --")
    ro = make_snippet("ports2", ["show interfaces status", "show ip arp"])

    first = collection.collect(session_for("sw2", FakeChannel({
        "show interfaces status": "Gi1/0/1  connected\nGi1/0/2  notconnect",
        "show ip arp": "10.0.0.1  aabb.cc00.0100",
    })), [ro.id], "run-1")
    time.sleep(0.05)

    # Edit the snippet between runs: a new command first, so positions shift.
    ro2 = snippets.save_snippet({"id": ro.id, "name": "ports2",
                                 "commands": ["show ip arp", "show interfaces status",
                                              "show version"]})
    second = collection.collect(session_for("sw2", FakeChannel({
        "show interfaces status": "Gi1/0/1  connected\nGi1/0/2  err-disabled",
        "show ip arp": "10.0.0.1  aabb.cc00.0100",
        "show version": "Version 15.2",
    })), [ro2.id], "run-2")

    report = collection.compare(second["session_id"])
    by = {c["command"]: c for c in report["commands"]}
    check("the previous run was found", report["previous"] == first["session_id"], str(report))
    check("the changed command is changed",
          by["show interfaces status"]["state"] == "changed", str(by))
    check("with a real diff in it",
          "-Gi1/0/2  notconnect" in by["show interfaces status"]["diff"]
          and "+Gi1/0/2  err-disabled" in by["show interfaces status"]["diff"])
    check("the unchanged one is the same, despite moving position",
          by["show ip arp"]["state"] == "same",
          "matched by command text, not position")
    check("the added one is new, not changed",
          by["show version"]["state"] == "new")
    check("the summary counts", report["changed"] == 1 and "1 command changed" in report["summary"],
          report["summary"])

    first_report = collection.compare(first["session_id"])
    check("the first run says there is nothing before it",
          first_report["previous"] is None and "first collection" in first_report["summary"])
    check("a live session is refused",
          _raises(lambda: collection.compare("live-sw2"), collection.CollectionError))


def test_bounded() -> None:
    print("\n-- Bounded --")
    check("the retention setting defaults to a month of nightly runs",
          advanced.get("history.collection_keep") == 30,
          str(advanced.get("history.collection_keep")))
    # The floor, so the sweep is exercised in a handful of runs rather than
    # thirty-three of them at the settle time each.
    advanced.update({"history.collection_keep": 2})
    keep = int(advanced.get("history.collection_keep"))
    check("and it is clamped at the bottom", keep == 2, str(keep))

    ro = make_snippet("one", ["show clock"])
    for i in range(keep + 3):
        collection.collect(session_for("sw3", FakeChannel({"show clock": f"t{i}"})),
                           [ro.id], f"r{i:03d}")
    runs = collection.runs_for("sw3", keep + 50)
    check(f"at most {keep} runs are kept per device", len(runs) == keep, str(len(runs)))
    check("and the newest survive",
          any("r%03d" % (keep + 2) in r["label"] for r in runs))

    huge = "x" * (int(advanced.get("history.max_output_chars")) + 5000)
    out = collection.collect(session_for("sw4", FakeChannel({"show clock": huge})),
                             [ro.id], "big")
    stored = store.get_session(out["session_id"])["commands"][0]["output"]
    check("output is capped by the history setting",
          len(stored) <= int(advanced.get("history.max_output_chars")) + 40
          and "truncated" in stored, str(len(stored)))


def test_the_scheduler_runs_it_after_the_capture() -> None:
    print("\n-- On the timer --")
    calls: list = []
    profiles = [{"id": "p1", "name": "sw5", "hostname": "sw5",
                 "connection_type": "ssh", "has_saved_credentials": True}]

    result = scheduler.run_group(
        "g", profiles,
        connect=lambda p: {"session_id": "h", "hostname": "sw5"},
        capture=lambda s: calls.append("capture") or {"stored": True},
        open_session_for=lambda p: None,
        destroy=lambda s: calls.append("destroy"),
        collect=lambda s, ids: calls.append(("collect", tuple(ids))),
        collect_ids=["a"],
    )
    check("capture, then collect, then the session is closed",
          calls == ["capture", ("collect", ("a",)), "destroy"], str(calls))
    check("the device is recorded as collected",
          result["collected"] == ["sw5"], str(result))

    def boom(s, ids):
        raise RuntimeError("no channel")

    result = scheduler.run_group(
        "g", profiles,
        connect=lambda p: {"session_id": "h", "hostname": "sw5"},
        capture=lambda s: {"stored": False},
        open_session_for=lambda p: None, destroy=lambda s: None,
        collect=boom, collect_ids=["a"],
    )
    check("a collection that fails does not fail the backup",
          result["ok"] == ["sw5"] and result["collect_failed"]
          and result["collect_failed"][0]["why"] == "no channel",
          str(result))

    check("run_now reads the schedule's collect list",
          re.search(r"collect_ids = list\(plan\.get\(\"collect\"\)", SCHED) is not None)
    check("and there is nothing to collect when none is chosen",
          "collect=_collect if collect_ids else None" in SCHED)


def test_the_interface() -> None:
    print("\n-- The dialog and the panel --")

    check("the schedule dialog lists what may be collected",
          "/api/collection/snippets" in GROUPS_JS)
    check("and names the ones it will not, with the reason",
          "Not offered for collection" in GROUPS_JS,
          "a list that silently omitted the write snippets would have "
          "somebody wondering where theirs went")
    check("the choice is saved on the schedule",
          "collect," in GROUPS_JS.split("_update(group.key, { backup:")[1][:200])

    check("History has a kind filter", 'id="history-kind"' in HTML)
    check("with the three answers to 'who ran this'",
          'value="live"' in HTML and 'value="collection"' in HTML)
    check("and it is sent with the search", "params.set('kind'" in HISTORY_JS)
    check("a collection run offers a comparison",
          "Compare with the previous run" in HISTORY_JS)
    check("changed commands come first and open",
          "order = { changed: 0, new: 1, same: 2 }" in HISTORY_JS
          and "item.open = entry.state === 'changed'" in HISTORY_JS)
    check("the diff is text, never markup",
          "pre.textContent = entry.diff" in HISTORY_JS)

    check("the routes exist",
          '@app.get("/api/collection/snippets")' in APP
          and '@app.get("/api/collection/{session_id}/compare")' in APP)
    check("and none of them runs a command",
          "collect(" not in APP.split("REST — Scheduled show collection")[1]
          .split("REST — Crash reports")[0],
          "collection happens inside the scheduler, on the schedule")


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def main() -> int:
    print("=" * 52)
    print("  Scheduled show collection")
    print("=" * 52)

    for test in (
        test_what_may_be_scheduled,
        test_a_run_lands_in_history,
        test_compare_with_the_previous_run,
        test_bounded,
        test_the_scheduler_runs_it_after_the_capture,
        test_the_interface,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    try:
        store.close()
    except Exception:
        pass
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
