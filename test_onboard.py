"""
test_onboard.py — What ShellMate types into a session before you have.

Two things reach a device on the user's behalf in the first seconds of a
session: the platform's paging-off command, and — since #532 — the saved
connection's own on-connect script. Both are governed by the same two rules,
and both rules are the kind that fail silently when they break.

**Nothing is guessed.** A line goes out only when the device is idle at a bare
prompt, one line per prompt. The subtle half is that "at a prompt" is a fact
about output that has *already arrived*: in the half-second after a line is
sent, the transcript still describes the prompt before it. A runner that
believed that fired its whole script into a device that had answered none of
it — which is the failure this file exists to prevent.

**Nothing is sent silently.** What was sent, what was not, and why, all come
back in the summary, because the person whose session it is has to be able to
account for it afterwards.

The enable password gets its own attention. It is answered once, at a prompt
we went looking for, within a deadline — the same shape as telnet auto-login,
and for the same reason: a password prompt regex left armed will eventually
match ordinary output and type a password into a live device.

    python test_onboard.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-onboard-"))
paths._data_dir_cache = _TEMP

from backend.onboard import (ENABLE_ANSWER_DEADLINE, ON_CONNECT_DEADLINE,  # noqa: E402
                             OnConnectScript)

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


def test_one_line_per_prompt() -> None:
    print("\n-- One line per prompt --")
    script = OnConnectScript(lines=["terminal monitor", "terminal width 200"])

    check("nothing goes out while the device is busy",
          script.next_line(at_prompt=False) is None)
    check("the first line goes at the prompt",
          script.next_line(at_prompt=True) == "terminal monitor")

    # The important one. The transcript has not been fed anything since, so
    # "at a prompt" still describes the prompt the *first* line was sent at.
    check("the second does not follow it into a silent device",
          script.next_line(at_prompt=True) is None,
          "the whole script would land in one keystroke's worth of time")

    script.observe("terminal monitor\r\nsw1#")
    check("  and does once the device has answered",
          script.next_line(at_prompt=True) == "terminal width 200")

    script.observe("sw1#")
    check("then the script is finished", script.next_line(at_prompt=True) is None)
    check("  and says so", script.done)
    summary = script.summary()
    check("  having sent both lines",
          summary["sent"] == ["terminal monitor", "terminal width 200"], str(summary))
    check("  and skipped none", summary["skipped"] == [] and summary["reason"] == "",
          str(summary))


def test_a_device_that_stops_answering() -> None:
    print("\n-- A device that stops answering --")
    script = OnConnectScript(lines=["terminal monitor", "terminal width 200"])
    check("the first line goes", script.next_line(at_prompt=True) == "terminal monitor")

    # A device mid-reload, or one that never prints a prompt again. The rest
    # must not be fired at it thirty seconds later, into whatever the user
    # has started doing by then.
    late = script.started_at + ON_CONNECT_DEADLINE + 1
    check("nothing more goes out while it is quiet",
          script.next_line(at_prompt=False, now=late) is None)
    check("and the script gives up rather than waiting for a gap", script.done)

    summary = script.summary()
    check("  naming what did not go", summary["skipped"] == ["terminal width 200"],
          str(summary))
    check("  and why", summary["reason"] == "no-prompt", str(summary))


def test_the_enable_password() -> None:
    print("\n-- enable --")
    script = OnConnectScript(lines=["enable", "terminal monitor"])
    check("the enable line goes like any other",
          script.next_line(at_prompt=True) == "enable")
    check("  and the script then waits for the password prompt",
          script.awaiting_password)

    check("ordinary output is not a password prompt",
          script.observe("enable\r\n") is False)
    check("  and the next line does not go out meanwhile",
          script.next_line(at_prompt=False) is None)

    check("the device asking is what asks for the password",
          script.observe("Password: ") is True)
    check("  and it disarms itself immediately",
          not script.awaiting_password,
          "a prompt pattern left armed will match ordinary output an hour "
          "into a session and type a password into a live device")
    check("  so a second Password: later is not answered again",
          script.observe("Password: ") is False)

    script.answered()
    check("the line after it waits for the device to accept it",
          script.next_line(at_prompt=True) is None)
    script.observe("\r\nsw1#")
    check("  and then goes", script.next_line(at_prompt=True) == "terminal monitor")


def test_a_device_that_was_already_privileged() -> None:
    print("\n-- enable, on a session that did not need it --")
    script = OnConnectScript(lines=["enable", "terminal monitor"])
    script.next_line(at_prompt=True)
    check("the script is waiting for a password prompt", script.awaiting_password)

    # The device came straight back to a prompt: it never asked. Carrying on
    # is right; sitting out the deadline would stall the rest of the script
    # for eight seconds on every connection to an already-enabled session.
    script.observe("enable\r\nsw1#")
    check("a prompt instead of a question carries on",
          script.next_line(at_prompt=True) == "terminal monitor")
    check("  and the wait is over", not script.awaiting_password)


def test_a_device_that_never_asks_and_never_answers() -> None:
    print("\n-- enable, and then nothing --")
    script = OnConnectScript(lines=["enable", "terminal monitor"])
    script.next_line(at_prompt=True)

    late = script.started_at + ENABLE_ANSWER_DEADLINE + 1
    check("the wait for a password prompt is bounded",
          script.next_line(at_prompt=False, now=late) is None)
    check("  and the script stops rather than waiting forever", script.done)
    check("  saying which of the two silences it was",
          script.summary()["reason"] == "no-password-prompt",
          str(script.summary()))


def test_it_waits_for_the_paging_command_too() -> None:
    """
    The script must not land on top of onboarding's own command.

    Paging-off is sent at a prompt, and for the half-second before the device
    echoes it the transcript still says the session is idle at that same
    prompt. A script that started there would send its first line into a
    device that had not answered the command before it — the same race as
    between two of its own lines, arriving from a different direction.
    """
    print("\n-- After the paging command --")
    script = OnConnectScript(lines=["terminal monitor"])
    script.wait_for_device()

    check("nothing goes out at a prompt that predates the paging command",
          script.next_line(at_prompt=True) is None)
    script.observe("terminal length 0\r\nsw1#")
    check("  and the first line goes once the device has answered it",
          script.next_line(at_prompt=True) == "terminal monitor")


def test_the_summary_is_honest() -> None:
    print("\n-- Saying what happened --")
    script = OnConnectScript(lines=["one", "two", "three"])
    script.next_line(at_prompt=True)
    script.finish("disconnected")

    summary = script.summary()
    check("a session that dropped mid-script says so",
          summary["reason"] == "disconnected", str(summary))
    check("  with one line sent", summary["sent"] == ["one"], str(summary))
    check("  and two named as not sent", summary["skipped"] == ["two", "three"],
          str(summary))
    check("nothing more goes out afterwards",
          script.next_line(at_prompt=True) is None)


def test_an_empty_script_does_nothing() -> None:
    print("\n-- Nothing to send --")
    script = OnConnectScript(lines=[])
    check("an empty script sends nothing", script.next_line(at_prompt=True) is None)
    check("  and finishes at once", script.done)
    check("  with nothing to report",
          script.summary() == {"sent": [], "skipped": [], "reason": ""},
          str(script.summary()))


def main() -> int:
    print("=" * 52)
    print("  Onboarding and the on-connect script")
    print("=" * 52)
    for test in (
        test_one_line_per_prompt,
        test_a_device_that_stops_answering,
        test_the_enable_password,
        test_a_device_that_was_already_privileged,
        test_a_device_that_never_asks_and_never_answers,
        test_it_waits_for_the_paging_command_too,
        test_the_summary_is_honest,
        test_an_empty_script_does_nothing,
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
