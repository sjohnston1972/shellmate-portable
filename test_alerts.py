"""
test_alerts.py — Tests for pending-reload and commit-confirm tracking.

The failures worth catching here are the quiet ones. A countdown that keeps
running after `reload cancel` is worse than no countdown at all, and one built
from a misread command is worse still — someone will trust it and walk away.

    python test_alerts.py
"""

import sys
import time

from backend.alerts import (
    COMMIT_CONFIRM, RELOAD, AlertTracker, WatchTracker,
    _seconds_until_clock_time,
)

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


def about(tracker, seconds, tolerance=5) -> bool:
    left = tracker.pending.seconds_left() if tracker.pending else None
    return left is not None and abs(left - seconds) <= tolerance


# ---------------------------------------------------------------------------


def test_typed_reload() -> None:
    """A reload typed by the user is noticed as soon as Enter is pressed."""
    print("\n-- Typed reload --")
    t = AlertTracker(platform="ios")

    check("nothing pending to begin with", t.pending is None)

    check("`reload in 10` is noticed", t.observe_command("reload in 10"))
    check("as a reload", t.pending.kind == RELOAD, str(t.pending))
    check("ten minutes out", about(t, 600), str(t.pending.seconds_left()))
    check("countable", t.pending.confident)
    check("but not yet the device's own word", not t.pending.authoritative)

    t2 = AlertTracker(platform="ios")
    t2.observe_command("reload in 1:30")
    check("`reload in 1:30` is an hour and a half", about(t2, 5400),
          str(t2.pending.seconds_left()))

    t3 = AlertTracker(platform="ios")
    t3.observe_command("show running-config")
    check("an ordinary command schedules nothing", t3.pending is None)

    t4 = AlertTracker(platform="ios")
    t4.observe_command("show reload")
    check("`show reload` is not a reload", t4.pending is None, str(t4.pending))


def test_device_word_wins() -> None:
    """The device's own banner is authoritative and re-synchronises the clock."""
    print("\n-- The device's own word --")
    t = AlertTracker(platform="ios")
    t.observe_command("reload in 10")

    changed = t.observe_output("*** --- SHUTDOWN in 0:05:00 ---")
    check("the banner is read", changed)
    check("and overrides our estimate", about(t, 300), str(t.pending.seconds_left()))
    check("now marked authoritative", t.pending.authoritative)

    # IOS repeats the banner. Re-reporting an unchanged deadline would redraw
    # the interface for nothing.
    check("an unchanged repeat is not reported",
          not t.observe_output("*** --- SHUTDOWN in 0:05:00 ---"))

    check("a changed repeat is reported",
          t.observe_output("*** --- SHUTDOWN in 0:01:00 ---"))
    check("and moves the deadline", about(t, 60), str(t.pending.seconds_left()))

    # Our own re-reading must not undo a better number from the device.
    t.observe_output("Reload scheduled for 23:00:00 (in 45 minutes)")
    before = t.pending.seconds_left()
    t.observe_command("reload in 10")
    check("a stale estimate cannot overwrite the device's",
          abs(t.pending.seconds_left() - before) < 5,
          f"{before} -> {t.pending.seconds_left()}")


def test_cancelling() -> None:
    """A cancelled reload must stop counting down."""
    print("\n-- Cancelling --")
    t = AlertTracker(platform="ios")
    t.observe_command("reload in 10")
    check("pending before", t.pending is not None)

    check("`reload cancel` is noticed", t.observe_command("reload cancel"))
    check("nothing pending after", t.pending is None)

    t2 = AlertTracker(platform="ios")
    t2.observe_command("reload in 10")
    check("the device's abort notice is noticed",
          t2.observe_output("*** --- SHUTDOWN ABORTED ---"))
    check("nothing pending after that either", t2.pending is None)

    check("cancelling nothing changes nothing",
          not AlertTracker(platform="ios").observe_command("reload cancel"))


def test_commit_confirmed() -> None:
    """Junos commit-confirm is tracked separately from a reboot."""
    print("\n-- Junos commit confirmed --")
    t = AlertTracker(platform="junos")

    check("`commit confirmed` is noticed", t.observe_command("commit confirmed"))
    check("as a commit-confirm, not a reload", t.pending.kind == COMMIT_CONFIRM,
          str(t.pending))
    check("defaulting to ten minutes", about(t, 600), str(t.pending.seconds_left()))

    check("a new number replaces the old deadline",
          t.observe_command("commit confirmed 3"))
    check("three minutes now", about(t, 180), str(t.pending.seconds_left()))

    check("a plain `commit` confirms it", t.observe_command("commit"))
    check("so nothing is pending", t.pending is None)

    t2 = AlertTracker(platform="junos")
    t2.observe_command("commit confirmed 5")
    check("the device's rollback notice is read",
          t2.observe_output("commit confirmed will be automatically rolled back "
                            "in 2 minutes unless confirmed"))
    check("and it wins", about(t2, 120), str(t2.pending.seconds_left()))


def test_unidentified_device() -> None:
    """No platform means no guessing."""
    print("\n-- An unidentified device --")
    t = AlertTracker()
    check("nothing is tracked before identification",
          not t.observe_command("reload in 10"))
    check("and nothing is pending", t.pending is None)

    generic = AlertTracker(platform="generic")
    generic.observe_command("reload in 10")
    check("the generic profile schedules nothing either", generic.pending is None,
          str(generic.pending))


def test_expiry() -> None:
    """A deadline that has passed stops being shown."""
    print("\n-- Expiry --")
    t = AlertTracker(platform="ios")
    t.observe_command("reload in 10")
    check("not expired while it is still ahead", not t.expire())

    t.pending.deadline = time.time() - 120
    check("expired once well past", t.expire())
    check("and cleared", t.pending is None)

    stale = AlertTracker(platform="ios")
    stale.observe_command("reload at 23:00")
    stale.pending.requested_at = time.time() - (7 * 3600)
    check("a very old pending action is dropped", stale.expire())
    check("even though its deadline is ahead", stale.pending is None)


def test_absolute_times() -> None:
    """`reload at 23:00` resolves to the next 23:00, not a negative number."""
    print("\n-- Absolute times --")
    seconds = _seconds_until_clock_time("23:00")
    check("a clock time resolves", seconds is not None)
    check("to somewhere in the next day",
          0 < seconds <= 24 * 3600, str(seconds))

    check("nonsense is refused", _seconds_until_clock_time("99:99") is None)
    check("so is a non-time", _seconds_until_clock_time("later") is None)

    t = AlertTracker(platform="ios")
    t.observe_command("reload at 23:00")
    check("`reload at` schedules something", t.pending is not None)
    check("with a deadline ahead of now", t.pending.seconds_left() > 0)


def test_bad_pattern_is_survivable() -> None:
    """A broken regex in an edited platforms.json must not break the session."""
    print("\n-- A broken user pattern --")
    from backend.platforms import PlatformProfile

    t = AlertTracker(platform="ios")
    broken = PlatformProfile(id="ios", name="x", reload_patterns=["(unclosed"])
    t._profile = lambda: broken                      # noqa: SLF001 — test seam

    try:
        result = t.observe_command("reload in 10")
        check("a bad pattern is skipped rather than raising", result is False)
    except Exception as exc:
        check("a bad pattern is skipped rather than raising", False, repr(exc))


def test_payload() -> None:
    """What reaches the browser carries the deadline, not a duration."""
    print("\n-- The payload --")
    t = AlertTracker(platform="ios")
    empty = t.payload()
    check("nothing pending is reported as such", empty["pending"] is None, str(empty))

    t.observe_command("reload in 10")
    payload = t.payload()["pending"]
    check("the kind is reported", payload["kind"] == RELOAD, str(payload))
    # IOS announces its reloads, so the typed command alone is tracked but
    # not counted down from (#248): the command can still be aborted at the
    # device's own [confirm] prompt.
    check("a typed reload awaits the device's confirmation",
          payload["awaiting_confirmation"] is True, str(payload))
    check("and shows no deadline until it comes",
          payload["deadline_ms"] is None, str(payload))
    check("the command that caused it is included",
          "reload in 10" in payload["source"], str(payload))

    # The scheduling line is printed *before* the [confirm] prompt, so it
    # says what the device was asked to do, not what it is doing (#291).
    t.observe_output("Reload scheduled in 10 minutes by steven on vty0")
    t.observe_output("Proceed with reload? [confirm]")
    asked = t.payload()["pending"]
    check("the scheduling line does not arm the countdown",
          asked["awaiting_confirmation"] is True and asked["deadline_ms"] is None,
          str(asked))

    # The banner only appears once the reload is genuinely armed.
    t.observe_output("*** --- SHUTDOWN in 0:10:00 ---")
    confirmed = t.payload()["pending"]
    check("the SHUTDOWN banner arms the countdown",
          confirmed["awaiting_confirmation"] is False, str(confirmed))
    check("as an absolute deadline in milliseconds",
          confirmed["deadline_ms"] is not None
          and confirmed["deadline_ms"] > time.time() * 1000, str(confirmed))
    check("and the way to call it off travels with it",
          confirmed.get("cancel_command") == "reload cancel", str(confirmed))

    # And the device's own abort clears it.
    t.observe_output("*** --- SHUTDOWN ABORTED ---")
    check("SHUTDOWN ABORTED stops the tracking",
          t.payload()["pending"] is None, str(t.payload()))

    # Declining the guardrail retracts a pending the device never confirmed.
    t2 = AlertTracker(platform="ios")
    t2.observe_command("reload in 10")
    check("a declined command's pending retracts", t2.retract() is True)
    check("and nothing is left behind", t2.payload()["pending"] is None)


def test_the_cancel_command_travels_with_the_pending() -> None:
    """
    The way out reaches the interface, and only where it is real.

    Clearing ShellMate's countdown is not cancelling the reload (#584). The
    tab menu sends the *platform's* own command, which it can only do if the
    platform's command is what arrived with the pending — and where a
    platform has none the field must be empty, so the entry can say so
    rather than typing an IOS command at something that is not IOS.
    """
    print("\n-- The cancel command --")
    from backend.platforms import PlatformProfile, get_profile

    expected = {
        "ios":    "reload cancel",
        "nxos":   "reload cancel",
        "junos":  "clear system reboot",
        "huawei": "undo schedule reboot",
    }
    for platform, command in expected.items():
        check(f"{platform} knows how to call a reload off",
              get_profile(platform).reload_cancel_command == command,
              f"got {get_profile(platform).reload_cancel_command!r}")

    # Every platform ShellMate reads a scheduled reload on must have one, or
    # the menu entry is permanently disabled on a device it could have saved.
    for platform_id in ("ios", "nxos", "junos", "huawei"):
        profile = get_profile(platform_id)
        check(f"{platform_id} tracks reloads and can cancel them",
              bool(profile.reload_patterns) and bool(profile.reload_cancel_command),
              f"patterns={len(profile.reload_patterns)}, "
              f"cancel={profile.reload_cancel_command!r}")

    t = AlertTracker(platform="ios")
    t.observe_command("reload in 10")
    t.observe_output("*** --- SHUTDOWN in 0:10:00 ---")
    check("it is sent with the pending, not looked up in the browser",
          t.payload()["pending"]["cancel_command"] == "reload cancel",
          str(t.payload()))

    # A commit confirm is not a reload and has no reload cancel command; the
    # menu says which, rather than offering `reload cancel` for a rollback.
    c = AlertTracker(platform="junos")
    c.observe_command("commit confirmed 5")
    pending = c.payload()["pending"]
    check("a pending rollback carries no reload cancel command",
          pending["kind"] == COMMIT_CONFIRM and "cancel_command" not in pending,
          str(pending))

    # And the case the disabled menu entry exists for: a platform that reads
    # a reload but has no command to call it off. Reachable today only
    # through an edited platforms.json, which is exactly why it must not
    # fall back to somebody else's command.
    mute = AlertTracker(platform="ios")
    mute._profile = lambda: PlatformProfile(                 # noqa: SLF001
        id="homegrown", name="Homegrown",
        reload_patterns=[r"^\s*reload\s+in\s+(?P<m>\d+)\b"])
    mute.observe_command("reload in 10")
    check("a platform with no cancel command sends an empty one",
          mute.payload()["pending"]["cancel_command"] == "",
          str(mute.payload()))


def test_output_watch() -> None:
    """A colour rule marked "alert" watches the output for it (#521)."""
    print("\n-- Output watch --")

    w = WatchTracker()
    check("nothing is watched by default", w.load(None) == 0)
    check("and nothing matches", w.observe("%LINK-3-UPDOWN\n") == [])

    armed = w.load({"enabled": True, "rules": [
        {"pattern": r"%LINK-3-UPDOWN", "colour": "red", "alert": True,
         "severity": "critical", "cooldown_s": 30},
        # No alert flag: colour only, and no business interrupting anyone.
        {"pattern": "interface", "colour": "blue"},
    ]})
    check("only the rules marked alert are armed", armed == 1, str(armed))

    hits = w.observe("%LINK-3-UPDOWN: Interface Gi0/2, changed state to down\n")
    check("a matching line is reported", len(hits) == 1, str(hits))
    check("with its severity", hits and hits[0]["severity"] == "critical", str(hits))
    check("and the line it matched",
          hits and "Gi0/2" in hits[0]["line"], str(hits))

    check("a colour-only rule raises nothing",
          w.observe("interface GigabitEthernet0/1\n") == [])

    # The reason the cooldown exists: a flap, or a chatty debug.
    check("the same rule does not fire again inside its cooldown",
          w.observe("%LINK-3-UPDOWN: Interface Gi0/3, changed state to up\n") == [])

    # Escape sequences must not be able to hide a match. A device colouring
    # its own log lines splits the keyword with SGR codes.
    w2 = WatchTracker()
    w2.load({"enabled": True, "rules": [
        {"pattern": "err-disabled", "alert": True, "cooldown_s": 0}]})
    coloured = "Gi0/5 is \x1b[31merr-disabled\x1b[0m (bpduguard)\n"
    check("a match survives the device's own colour",
          len(w2.observe(coloured)) == 1, str(w2.observe(coloured)))

    # A line split across two chunks is still one line.
    w3 = WatchTracker()
    w3.load({"enabled": True, "rules": [
        {"pattern": "BGP.*Down", "alert": True, "ignore_case": False}]})
    check("half a line matches nothing yet", w3.observe("%BGP-5-ADJCHANGE: neighbour ") == [])
    check("and matches once the rest arrives",
          len(w3.observe("10.0.0.1 Down\n")) == 1)

    # A broken pattern is skipped, not fatal — settings.json is hand-editable.
    w4 = WatchTracker()
    check("an unparseable pattern is skipped",
          w4.load({"enabled": True, "rules": [
              {"pattern": "(unclosed", "alert": True},
              {"pattern": "reload", "alert": True}]}) == 1)

    # One switch for one feature: colouring off means watching off.
    w5 = WatchTracker()
    check("highlighting off disarms the watch too",
          w5.load({"enabled": False, "rules": [
              {"pattern": "down", "alert": True}]}) == 0)

    # Severity is a fixed set; anything else is the middle of the road.
    w6 = WatchTracker()
    w6.load({"enabled": True, "rules": [
        {"pattern": "down", "alert": True, "severity": "catastrophic"}]})
    check("an unknown severity falls back to warning",
          w6.rules[0].severity == "warning", w6.rules[0].severity)

    # A cooldown out of range is clamped rather than obeyed.
    w7 = WatchTracker()
    w7.load({"enabled": True, "rules": [
        {"pattern": "down", "alert": True, "cooldown_s": 99999}]})
    check("an absurd cooldown is clamped", w7.rules[0].cooldown_s == 3600.0,
          str(w7.rules[0].cooldown_s))


def main() -> int:
    print("=" * 52)
    print("  Alert tracking")
    print("=" * 52)

    for test in (
        test_typed_reload,
        test_device_word_wins,
        test_cancelling,
        test_commit_confirmed,
        test_unidentified_device,
        test_expiry,
        test_absolute_times,
        test_bad_pattern_is_survivable,
        test_payload,
        test_the_cancel_command_travels_with_the_pending,
        test_output_watch,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

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
