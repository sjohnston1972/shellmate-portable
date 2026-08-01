"""
test_live_capture.py — Capturing a configuration through the live session.

The state machine is driven here the way the terminal read loop drives it —
ready_to_start / started / feed / tick — because that loop owns the channel
and this class never reads from one.

What is being protected is mostly negative: a capture must not be mistaken for
something the user did. So alongside "does it get the configuration" there are
checks that it does not begin mid-command, does not survive a keystroke, and
does not run at all when switched off.

Run: python test_live_capture.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

# Redirected before anything imports the store — see test_clear_history.py,
# where getting this wrong emptied the real history database.
_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-livecap-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, (
    f"refusing to run: this would write to {paths.data_dir()}")

from backend import advanced, configs  # noqa: E402
from backend.configs import LiveCapture, capture_config_live  # noqa: E402
from backend.connections.base import ConnectionError_  # noqa: E402
from backend.session.transcript import TranscriptParser  # noqa: E402

passed = 0
failed = 0

CONFIG = (
    "Building configuration...\r\n\r\n"
    "Current configuration : 2841 bytes\r\n"
    "!\r\nversion 15.2\r\nhostname S3-R1\r\n!\r\n"
    "interface GigabitEthernet0/1\r\n description uplink\r\n!\r\nend\r\n"
)


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [ok]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  --  {detail}" if detail else ""))


def make(prompt: str = "S3-R1#", timeout: float = 5.0,
         settle: float = 0.2) -> LiveCapture:
    return LiveCapture(command="show running-config", prompt=prompt,
                       timeout=timeout, settle=settle)


print("\nIt does not begin until the device is idle at a prompt")
capture = make()
check("not ready mid-command", not capture.ready_to_start(at_prompt=False))
check("ready at a prompt", capture.ready_to_start(at_prompt=True))
capture.started()
check("only once", not capture.ready_to_start(at_prompt=True))
check("active once started", capture.active)

print("\nA whole configuration, arriving in pieces")
capture = make()
capture.started()
# The device echoes the command first, which is why the prompt in that line
# cannot be allowed to end the capture.
capture.feed("show running-config\r\n")
check("the echoed command does not end it", capture.active)
for i in range(0, len(CONFIG), 40):
    capture.feed(CONFIG[i:i + 40])
check("still running before the prompt returns", capture.active)
capture.feed("S3-R1#")
check("the prompt ends it", capture.state == "done", capture.state)
check("the configuration is intact", "hostname S3-R1" in capture.text)
check("and complete", "end" in capture.text and "GigabitEthernet0/1" in capture.text)

print("\nA pager is answered, not disabled")
# The second channel gets a 200x1000 PTY so paging never engages. The live
# session has the user's own geometry, so it can. Sending `terminal length 0`
# would change their session's state and outlast the capture.
capture = make()
capture.started()
capture.feed("show running-config\r\n")
answer = capture.feed("interface GigabitEthernet0/1\r\n --More-- ")
check("a space is sent back", answer == " ", repr(answer))
check("still running", capture.active)
answer = capture.feed("\r\nhostname S3-R1\r\n")
check("nothing sent when there is no pager", answer is None, repr(answer))
# A device erases its own pager prompt with backspaces once answered, so the
# marker must not be re-answered on the next chunk — that was a stream of
# stray spaces into a live session.
answer = capture.feed(chr(8) + chr(8) + " more config\r\n")
check("the answered pager is not answered again", answer is None, repr(answer))

for marker in ("<--- More --->", "---(more)---", "--more--",
               "--More(50%)--", "---- More ----"):
    other = make()
    other.started()
    other.feed("show running-config\r\n")
    check(f"{marker} is recognised", other.feed("line\r\n" + marker) == " ")

print("\nIt gives up rather than hanging")
capture = make(timeout=0.05)
capture.started()
capture.feed("show running-config\r\n")
time.sleep(0.1)
capture.feed("more output with no prompt\r\n")
check("times out", capture.state == "aborted", capture.state)
check("says why", "timeout" in capture.reason, capture.reason)
check("and returns nothing rather than half a config", capture.text == "")

print("\nA device that goes quiet mid-configuration still finishes")
# No trailing prompt ever arrives — the quiet period is the only signal.
capture = make(settle=0.05)
capture.started()
capture.feed("show running-config\r\n")
capture.feed(CONFIG)
capture.tick()
check("not finished immediately", capture.active)
time.sleep(0.1)
capture.tick()
check("finished once quiet", capture.state == "done", capture.state)
check("with what did arrive", "hostname S3-R1" in capture.text)

print("\nTyping ends it immediately")
capture = make()
capture.started()
capture.feed("show running-config\r\n")
capture.feed("interface GigabitEthernet0/1\r\n")
capture.abort("you started typing")
check("aborted", capture.state == "aborted")
check("the waiter is released", capture.done.is_set())
check("no partial configuration is kept", capture.text == "")
check("a second abort is harmless", (capture.abort("again") or True))

print("\nThe waiting thread is released in every terminal state")
for finish in ("done", "aborted"):
    capture = make(settle=0.05)
    capture.started()
    capture.feed("show running-config\r\n")
    released = []
    thread = threading.Thread(
        target=lambda: released.append(capture.done.wait(3.0)), daemon=True)
    thread.start()
    if finish == "done":
        capture.feed(CONFIG + "S3-R1#")
    else:
        capture.abort("cancelled")
    thread.join(timeout=3.0)
    check(f"released on {finish}", released == [True], str(released))

print("\nIt refuses to run when switched off")
session = {
    "is_connected": True,
    "transcript": TranscriptParser(),
    "live_capture": None,
    "fingerprint": {"platform": "cisco_ios"},
}
original = advanced.get


def _off(key):
    return False if key == "capture.live_fallback" else original(key)


configs.advanced = _off
try:
    capture_config_live(session)
    check("raises when the setting is off", False, "no exception")
except ConnectionError_ as exc:
    check("raises when the setting is off", True)
    check("and names the setting", "switched off" in str(exc), str(exc))
finally:
    configs.advanced = original

print("\nIt refuses a second capture on the same session")
session["live_capture"] = make()
try:
    capture_config_live(session)
    check("refuses to overlap", False, "no exception")
except ConnectionError_ as exc:
    check("refuses to overlap", "already running" in str(exc), str(exc))
session["live_capture"] = None

print("\nA disconnected session is not typed into")
session["is_connected"] = False
try:
    capture_config_live(session)
    check("refuses when disconnected", False, "no exception")
except ConnectionError_ as exc:
    check("refuses when disconnected", "no longer connected" in str(exc), str(exc))

print("\n" + "=" * 52)
print(f"  {passed} passed  |  {failed} failed")
print("=" * 52)

sys.exit(1 if failed else 0)
