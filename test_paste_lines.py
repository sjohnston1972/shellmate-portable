"""
test_paste_lines.py — A pasted block sent through the real server (#523).

test_pipeline.py drives the state machine directly. This one runs the path
the user's paste actually takes: a real ShellMate session over a real
WebSocket, a `paste_lines` message, and a device on the other end that
answers with a prompt — or, in the second half, stops answering.

The device here is not SSH. That is the point: prompt-paced paste has to work
on a serial console, a telnet session and a device nothing could identify,
which is exactly where config push cannot go. A handler registered under a
made-up connection type is the cheapest honest stand-in for all three.

What matters:

- **One line per prompt.** A device that answers the first line and then goes
  quiet must receive *one* line, not the block. If the pacing were wrong all
  three would already be on the device before anyone noticed.
- **A stall is reported.** "line 1 sent, no prompt seen", with the rest
  counted as not sent, rather than a silent success.
- **A keystroke stops it**, the way it stops a live capture.

    python test_paste_lines.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-paste-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, (
    f"refusing to run: this would write to {paths.data_dir()}")

import uvicorn        # noqa: E402
import websockets     # noqa: E402

from backend.app import app, session_manager                       # noqa: E402
from backend.connections.base import ConnectionHandler, ConnectionParams  # noqa: E402
from backend.connections.manager import HANDLERS                   # noqa: E402

passed = 0
failed: list[str] = []

PROMPT = b"\r\nsw1#"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


class FakeDevice(ConnectionHandler):
    """
    A device that prints a prompt, and answers each line with another.

    ``answers`` is how many more lines it will answer; setting it to zero is
    a device that has stopped talking — a switch part-way through applying
    something, a console that has scrolled off into a pager.
    """

    #: Shared, because the session manager constructs the handler itself.
    latest: "FakeDevice | None" = None

    def __init__(self, params: ConnectionParams) -> None:
        self.params = params
        self.received: list[bytes] = []
        self.answers = 99
        self._pending: list[bytes] = []
        self._open = False
        FakeDevice.latest = self

    def connect(self) -> None:
        self._open = True
        self._pending.append(PROMPT)

    def send(self, data: bytes) -> None:
        self.received.append(data)
        if self.answers > 0 and data.endswith(b"\r"):
            self.answers -= 1
            self._pending.append(data + PROMPT)

    def recv(self, size: int = 4096) -> bytes | None:
        if self._pending:
            return self._pending.pop(0)
        # A real handler blocks with a timeout; without the sleep this would
        # spin the session's reader thread at 100% of a core.
        time.sleep(0.02)
        return None if self._open else b""

    def disconnect(self) -> None:
        self._open = False

    @property
    def is_connected(self) -> bool:
        return self._open


HANDLERS["fake"] = FakeDevice

PORT = 0
with __import__("socket").socket() as _probe:
    _probe.bind(("127.0.0.1", 0))
    PORT = _probe.getsockname()[1]

_server = uvicorn.Server(uvicorn.Config(
    app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=_server.run, daemon=True).start()
time.sleep(2)


def _open_session() -> tuple[str, FakeDevice]:
    session_manager.create_session(
        ConnectionParams(connection_type="fake", hostname="sw1"))
    session_id = list(session_manager._sessions)[-1]
    return session_id, FakeDevice.latest


def _lines_sent(device: FakeDevice) -> list[str]:
    """What the device was actually typed, as lines."""
    return [chunk.decode().strip() for chunk in device.received
            if chunk.strip()]


async def _run_batch(socket_, message: dict, seconds: float = 8.0) -> dict:
    """Send a batch and collect the report it ends with."""
    await socket_.send(json.dumps(message))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            reply = json.loads(await asyncio.wait_for(socket_.recv(), remaining))
        except asyncio.TimeoutError:
            break
        if reply.get("type") == "paste_batch" and reply.get("state") == "done":
            return reply
    return {}


async def test_a_line_at_a_time() -> None:
    print("\n-- A device that answers --")
    session_id, device = _open_session()
    async with websockets.connect(
            f"ws://127.0.0.1:{PORT}/ws/terminal/{session_id}",
            origin=f"http://127.0.0.1:{PORT}") as socket_:
        await asyncio.sleep(1.5)          # let the prompt land
        report = await _run_batch(socket_, {
            "type": "paste_lines", "mode": "prompt",
            "lines": ["interface Gi0/1", "description uplink",
                      "ip address 10.0.0.1 255.255.255.0"],
        })

    check("the batch reports back", bool(report), "no done message arrived")
    check("  every line was sent", report.get("sent") == 3, str(report))
    check("  with nothing left over", report.get("remaining") == 0, str(report))
    check("  and no stall", report.get("reason") == "", str(report))
    check("the device was typed the lines, in order",
          _lines_sent(device) == ["interface Gi0/1", "description uplink",
                                  "ip address 10.0.0.1 255.255.255.0"],
          str(_lines_sent(device)))
    session_manager.destroy_session(session_id)


async def test_a_held_line_pauses_the_batch() -> None:
    print("\n-- A line the guardrail holds --")
    session_id, device = _open_session()
    async with websockets.connect(
            f"ws://127.0.0.1:{PORT}/ws/terminal/{session_id}",
            origin=f"http://127.0.0.1:{PORT}") as socket_:
        await asyncio.sleep(1.5)
        await socket_.send(json.dumps({
            "type": "paste_lines", "mode": "prompt", "timeout_s": 30,
            "lines": ["show version", "reload", "show clock"],
        }))

        asked = {}
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not asked:
            try:
                reply = json.loads(await asyncio.wait_for(socket_.recv(), 1.0))
            except asyncio.TimeoutError:
                continue
            if reply.get("type") == "guardrail_prompt":
                asked = reply

        check("a `reload` in a pasted block is held, not sent",
              asked.get("command") == "reload", str(asked))
        # The batch is waiting on a person, so the line after it must not
        # have gone anywhere — the whole difference between pausing and
        # skipping past the question.
        await asyncio.sleep(1.0)
        check("  and the batch waits rather than carrying on",
              "show clock" not in _lines_sent(device), str(_lines_sent(device)))

        await socket_.send(json.dumps({
            "type": "guardrail_answer", "command": "reload", "confirmed": False}))
        report = await _run_batch(socket_, {"type": "keep_alive"}, seconds=8)

    check("  and it finishes once the question is answered",
          report.get("reason") == "" and report.get("remaining") == 0,
          str(report))
    check("  with the rest of the block sent",
          "show clock" in _lines_sent(device), str(_lines_sent(device)))
    session_manager.destroy_session(session_id)


async def test_a_device_that_stops_answering() -> None:
    print("\n-- A device that stops answering --")
    session_id, device = _open_session()
    async with websockets.connect(
            f"ws://127.0.0.1:{PORT}/ws/terminal/{session_id}",
            origin=f"http://127.0.0.1:{PORT}") as socket_:
        await asyncio.sleep(1.5)
        device.answers = 0                # it has stopped talking
        report = await _run_batch(socket_, {
            "type": "paste_lines", "mode": "prompt", "timeout_s": 2,
            "lines": ["line one", "line two", "line three"],
        })

    check("the batch stops rather than carrying on",
          report.get("reason") == "no-prompt", str(report))
    check("  naming the line that was never answered",
          report.get("stalled_at") == 1, str(report))
    check("  and counting what never went",
          report.get("remaining") == 2, str(report))
    check("the device really was sent only that one line",
          _lines_sent(device) == ["line one"], str(_lines_sent(device)))
    session_manager.destroy_session(session_id)


async def test_typing_stops_it() -> None:
    print("\n-- A keystroke stops the batch --")
    session_id, device = _open_session()
    async with websockets.connect(
            f"ws://127.0.0.1:{PORT}/ws/terminal/{session_id}",
            origin=f"http://127.0.0.1:{PORT}") as socket_:
        await asyncio.sleep(1.5)
        await socket_.send(json.dumps({
            "type": "paste_lines", "mode": "lines", "delay_ms": 400,
            "lines": ["one", "two", "three", "four", "five"],
        }))
        await asyncio.sleep(0.8)
        await socket_.send(json.dumps({"type": "input", "data": "s"}))

        report = {}
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                reply = json.loads(await asyncio.wait_for(socket_.recv(), 1.0))
            except asyncio.TimeoutError:
                break
            if reply.get("type") == "paste_batch" and reply.get("state") == "done":
                report = reply
                break

    check("it stops on the keystroke",
          report.get("reason") == "you started typing", str(report))
    check("  with the rest counted as not sent",
          report.get("remaining", 0) > 0, str(report))
    check("  and the device was not sent the whole block",
          len(_lines_sent(device)) < 5, str(_lines_sent(device)))
    session_manager.destroy_session(session_id)


async def main() -> int:
    print("=" * 52)
    print("  Pasting a block through the server")
    print("=" * 52)
    for test in (test_a_line_at_a_time,
                 test_a_held_line_pauses_the_batch,
                 test_a_device_that_stops_answering,
                 test_typing_stops_it):
        try:
            await test()
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
    code = asyncio.run(main())
    shutil.rmtree(_TEMP, ignore_errors=True)
    sys.exit(code)
