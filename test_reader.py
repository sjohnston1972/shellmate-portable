"""
test_reader.py — Device output reaches the browser through the session's
reader thread (#471), survives a browser going away and coming back (#344),
and no recv holds a slot of the loop's thread pool.

A tiny paramiko SSH server plays the device: it prints a prompt, echoes
what it is sent, and answers `show clock` with a line. The application is
driven through TestClient's WebSocket support. Run: python test_reader.py
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paramiko  # noqa: E402

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-reader-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from fastapi.testclient import TestClient  # noqa: E402
from backend.app import app  # noqa: E402

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


class EchoDevice(paramiko.ServerInterface):
    """Password login, then a shell that echoes and answers one command."""

    def __init__(self) -> None:
        self.keep: list = []

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args, **kwargs):
        return True

    def check_channel_shell_request(self, channel):
        self.keep.append(channel)

        def talk() -> None:
            channel.send(b"\r\nsw1#")
            line = b""
            while True:
                try:
                    data = channel.recv(1024)
                except Exception:
                    return
                if not data:
                    return
                channel.send(data)                       # echo
                line += data
                if b"\r" in line or b"\n" in line:
                    cmd = line.strip().decode(errors="replace")
                    line = b""
                    if cmd.startswith("show clock"):
                        channel.send(b"\r\n*10:00:00.000 UTC Wed Sep 3 2026\r\n")
                    channel.send(b"sw1#")
        threading.Thread(target=talk, daemon=True).start()
        return True


def start_device() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    server = EchoDevice()

    def run():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=server)
            except Exception:
                continue
            server.keep.append(transport)
    threading.Thread(target=run, daemon=True).start()
    return port


def collect(ws, needle: str, timeout: float = 8.0) -> str:
    """Read output messages until *needle* appears or time runs out."""
    seen = ""
    deadline = time.time() + timeout
    while time.time() < deadline and needle not in seen:
        try:
            msg = json.loads(ws.receive_text())
        except Exception:
            break
        if msg.get("type") == "output":
            seen += msg.get("data", "")
    return seen


def answer(ws, kind: str, timeout: float = 8.0) -> dict | None:
    """Read until a message of *kind* arrives; device output flows past."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = json.loads(ws.receive_text())
        except Exception:
            return None
        if msg.get("type") == kind:
            return msg
    return None


def main() -> int:
    print("=" * 52)
    print("  Reader thread")
    print("=" * 52)
    port = start_device()
    reader_names = lambda: [t.name for t in threading.enumerate() if t.name.startswith("reader-")]

    with TestClient(app, base_url="http://127.0.0.1") as client:
        res = client.post("/api/sessions", json={
            "hostname": "127.0.0.1", "port": port, "username": "eng", "password": "pw",
            "connection_type": "ssh", "display_label": "sw1"})
        check("a session opens against the fake device", res.status_code == 200, res.text[:200])
        sid = res.json()["session_id"]

        print("\n-- Output flows through the reader thread --")
        with client.websocket_connect(f"/ws/terminal/{sid}") as ws:
            seen = collect(ws, "sw1#")
            check("the prompt reaches the browser", "sw1#" in seen, repr(seen[-80:]))
            check("a reader thread exists for the session", any(sid[:8] in n for n in reader_names()), str(reader_names()))
            ws.send_text(json.dumps({"type": "input", "data": "show clock\r"}))
            seen = collect(ws, "UTC Wed Sep 3 2026")
            check("a command's output comes back", "UTC Wed Sep 3 2026" in seen, repr(seen[-120:]))

        print("\n-- A browser goes away and comes back --")
        time.sleep(0.3)
        with client.websocket_connect(f"/ws/terminal/{sid}") as ws:
            ws.send_text(json.dumps({"type": "input", "data": "show clock\r"}))
            seen = collect(ws, "UTC Wed Sep 3 2026")
            check("the session still answers after a reattach", "UTC Wed Sep 3 2026" in seen, repr(seen[-120:]))
            check("still one reader thread, not one per attach",
                  sum(1 for n in reader_names() if sid[:8] in n) == 1, str(reader_names()))

        # ------------------------------------------------------------------
        # Logging one session (#534)
        #
        # The global switch stays off throughout: the whole point of the
        # override is that "record this one, from now" must not start a file
        # for every other tab. So a file appearing at all here is the test.
        # ------------------------------------------------------------------
        print("\n-- Logging this one session --")
        from backend.settings_store import log_directory, update_settings
        update_settings({"logging": {"enabled": False}})

        with client.websocket_connect(f"/ws/terminal/{sid}") as ws:
            # With no `enabled` key it only asks — which is what a reattached
            # socket does, so that learning the state cannot wipe an override
            # the session is already carrying.
            ws.send_text(json.dumps({"type": "logging"}))
            state = answer(ws, "logging_state")
            check("a socket can ask whether its session is being logged",
                  state is not None and state.get("enabled") is False, str(state))

            ws.send_text(json.dumps({"type": "logging", "enabled": True}))
            state = answer(ws, "logging_state")
            check("one tab can turn it on for itself",
                  state is not None and state.get("enabled") is True, str(state))
            check("and is told which file it is writing to",
                  bool(state and state.get("filename", "").endswith(".log")), str(state))
            check("while the global setting is still off",
                  update_settings({}).get("logging", {}).get("enabled") is False)

            ws.send_text(json.dumps({"type": "input", "data": "show clock\r"}))
            collect(ws, "UTC Wed Sep 3 2026")

            written = log_directory() / state["filename"]
            deadline = time.time() + 5
            while time.time() < deadline and not written.exists():
                time.sleep(0.1)
            check("the file is written", written.exists(), str(written))
            check("with what the device said in it",
                  written.exists() and "show clock" in written.read_text(encoding="utf-8"),
                  written.read_text(encoding="utf-8")[-200:] if written.exists() else "")

            ws.send_text(json.dumps({"type": "logging", "enabled": False}))
            state = answer(ws, "logging_state")
            check("and it can be stopped again without touching the setting",
                  state is not None and state.get("enabled") is False, str(state))

        print("\n-- Closing the session ends the thread --")
        client.delete(f"/api/sessions/{sid}")
        deadline = time.time() + 5
        while time.time() < deadline and any(sid[:8] in n for n in reader_names()):
            time.sleep(0.1)
        check("the reader thread is gone", not any(sid[:8] in n for n in reader_names()), str(reader_names()))

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
