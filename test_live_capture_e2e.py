"""
test_live_capture_e2e.py — A live capture against a device that refuses a
second channel, driven through the real server.

The unit tests in test_live_capture.py drive the state machine directly. This
one runs the whole path: a paramiko SSH server that accepts one session
channel and rejects every one after it — which is precisely what a switch with
a concurrent-session limit does — a real ShellMate session over a real
WebSocket, and a real capture through it.

What matters here is mostly what must *not* happen. A capture through somebody
else's session is only acceptable if it is invisible while it runs, so the
assertions are that the configuration never reached the browser, the session
buffer, the transcript or the command history — and that the snapshot was
stored all the same.

Run: python test_live_capture_e2e.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-e2e-cap-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, (
    f"refusing to run: this would write to {paths.data_dir()}")

import httpx  # noqa: E402
import paramiko  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from backend.app import app  # noqa: E402
from backend.app import session_manager  # noqa: E402
from backend.store import store  # noqa: E402

passed = 0
failed = 0

PROMPT = "S3-R1#"
CONFIG_LINES = [
    "Building configuration...",
    "",
    "Current configuration : 1284 bytes",
    "!",
    "version 15.2",
    "hostname S3-R1",
    "!",
    "interface GigabitEthernet0/1",
    " description CAPTURED-OVER-LIVE-CHANNEL",
    " ip address 10.0.0.1 255.255.255.0",
    "!",
    "end",
]
CONFIG_TEXT = "\r\n".join(CONFIG_LINES) + "\r\n"

#: A string that appears only in the configuration, never in anything the user
#: typed. Every "did it leak" check looks for this one token.
MARKER = "CAPTURED-OVER-LIVE-CHANNEL"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [ok]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  --  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# A switch that allows exactly one channel
# ---------------------------------------------------------------------------

class OneChannelServer(paramiko.ServerInterface):
    """
    Accepts the first session channel and refuses the rest.

    This is the whole point of the fixture. `open_secondary_channel()` returns
    None against a device like this, and before #111 that meant configuration
    capture, drift detection and the diff feature never worked on it at all.
    """

    def __init__(self) -> None:
        self.shell_requested = threading.Event()
        self.channels_opened = 0

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind != "session":
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.channels_opened += 1
        if self.channels_opened > 1:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *args, **kwargs):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_requested.set()
        return True


def serve_shell(channel) -> None:
    """Answer `show running-config`, and echo anything else like a device."""
    channel.send(b"\r\n" + PROMPT.encode())
    buffer = b""
    while True:
        try:
            data = channel.recv(1024)
        except Exception:
            break
        if not data:
            break

        # Devices echo what is typed; the capture has to cope with its own
        # command coming back before any configuration does.
        channel.send(data)
        buffer += data
        if b"\r" not in buffer and b"\n" not in buffer:
            continue

        line = buffer.replace(b"\r", b"\n").split(b"\n")[0].decode(errors="replace").strip()
        buffer = b""
        channel.send(b"\r\n")
        if line.startswith("show running-config"):
            channel.send(CONFIG_TEXT.encode())
        elif line:
            channel.send(f"% Unknown command: {line}\r\n".encode())
        channel.send(PROMPT.encode())


def start_device() -> tuple[int, OneChannelServer]:
    """Bind a one-channel SSH device on a free port and return it."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    server = OneChannelServer()

    def run():
        while True:
            try:
                client, _ = listener.accept()
            except Exception:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=server)
            except Exception:
                continue
            channel = transport.accept(20)
            if channel is None:
                continue
            threading.Thread(target=serve_shell, args=(channel,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return port, server


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

DEVICE_PORT, device = start_device()

def _free_port() -> int:
    """A port nothing else holds: several suites run side by side."""
    import socket as _socket
    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


WEB_PORT = _free_port()
server = uvicorn.Server(uvicorn.Config(
    app, host="127.0.0.1", port=WEB_PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(3)

BASE = f"http://127.0.0.1:{WEB_PORT}"


async def run() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as http:
        response = await http.post("/api/sessions", json={
            "connection_type": "ssh",
            "hostname": "127.0.0.1",
            "port": DEVICE_PORT,
            "username": "neteng",
            "password": "letmein",
            "display_label": "S3-R1",
        })
        check("the session connects", response.status_code == 200,
              f"{response.status_code} {response.text[:200]}")
        if response.status_code != 200:
            return
        session_id = response.json()["session_id"]

        # Everything the browser would receive, collected as it arrives.
        seen: list[str] = []

        async with websockets.connect(
                f"ws://127.0.0.1:{WEB_PORT}/ws/terminal/{session_id}",
                origin=BASE) as socket_:

            async def drain():
                try:
                    while True:
                        message = json.loads(await socket_.recv())
                        if message.get("type") == "output":
                            seen.append(message["data"])
                except Exception:
                    pass

            reader = asyncio.create_task(drain())

            # Let the banner and prompt land, so the transcript has a prompt
            # to recognise — a capture must not start mid-command, and "at a
            # prompt" is how it knows.
            await asyncio.sleep(2.5)
            session = session_manager.get_session(session_id)
            check("the device presented a prompt",
                  bool(session["transcript"].last_prompt),
                  repr(session["transcript"].last_prompt))

            # A second channel is genuinely refused by this device — the
            # premise of the whole feature.
            handler = session["handler"]
            check("the device refuses a second channel",
                  handler.open_secondary_channel() is None)

            before = len(seen)
            result = await http.post(f"/api/sessions/{session_id}/snapshot")
            check("the capture succeeds anyway",
                  result.status_code == 200,
                  f"{result.status_code} {result.text[:300]}")

            if result.status_code == 200:
                body = result.json()
                check("it says it used the live session",
                      body.get("via") == "live session", str(body.get("via")))
                check("the configuration was stored",
                      body.get("line_count", 0) >= len(CONFIG_LINES) - 2,
                      str(body.get("line_count")))

            await asyncio.sleep(1.0)

            # --- the negatives, which are the point ---------------------
            during = "".join(seen[before:])
            check("nothing reached the browser", MARKER not in during,
                  repr(during[:160]))
            check("the command was not echoed to the browser",
                  "show running-config" not in during, repr(during[:160]))

            buffer_text = session["buffer"].get_text(5000)
            check("nothing reached the session buffer",
                  MARKER not in buffer_text, repr(buffer_text[-160:]))

            recorded = [c["command"] for c in
                        (store.get_session(session_id) or {}).get("commands", [])]
            check("it was not recorded as a command the user ran",
                  not any("running-config" in c for c in recorded), str(recorded))

            snapshot = store.latest_snapshot("S3-R1") or store.latest_snapshot("127.0.0.1")
            check("but the snapshot has the configuration",
                  bool(snapshot) and MARKER in snapshot.get("content", ""),
                  "no snapshot" if not snapshot else snapshot["content"][:80])

            # --- the session is still usable afterwards ------------------
            await socket_.send(json.dumps({"type": "input", "data": "show clock\r"}))
            await asyncio.sleep(1.5)
            after = "".join(seen)
            check("the user's own command still works",
                  "show clock" in after, repr(after[-200:]))

            check("the capture released the session",
                  session.get("live_capture") is None,
                  str(session.get("live_capture")))

            reader.cancel()

        await http.delete(f"/api/sessions/{session_id}")


asyncio.run(run())

print("\n" + "=" * 52)
print(f"  {passed} passed  |  {failed} failed")
print("=" * 52)

server.should_exit = True
time.sleep(0.5)
sys.exit(1 if failed else 0)
