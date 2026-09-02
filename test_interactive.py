"""
test_interactive.py — A device that asks a question only the user can answer.

Two-factor logins, TACACS one-time codes and "press Enter to continue"
banners all arrive as SSH keyboard-interactive prompts. Before #406 the
connect could only answer them with the password, and the failure read as
a wrong password. Now the first attempt reports what the device asked, and
the second attempt carries the answers.

The device here is an in-process paramiko server that wants a password
*and* a code. It checks that:

- the first attempt raises InteractiveRequired with the prompts, not a
  generic refusal
- the password prompt is answered from the password without asking
- a second attempt with the answers connects and opens a shell
- a wrong code is a clear refusal, not a request for the code again
- the API turns the same thing into a 409 the interface can act on

    python test_interactive.py
"""

import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import paramiko

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-interactive-"))
paths._data_dir_cache = _TEMP

from backend.connections.base import ConnectionError_, ConnectionParams, InteractiveRequired  # noqa: E402
from backend.connections.ssh_handler import SSHHandler                                          # noqa: E402

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


class TwoFactorServer(paramiko.ServerInterface):
    """Keyboard-interactive only: a password prompt, then a code prompt."""

    CODE = "424242"

    def __init__(self) -> None:
        self.seen_responses: list[list[str]] = []
        # Kept, or the channel is collected the moment accept() returns and
        # the client sees EOF before it can ask for a shell.
        self.keep: list = []

    def get_allowed_auths(self, username):
        return "keyboard-interactive"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        query = paramiko.InteractiveQuery("Two-factor login", "Enter your password and the code from your token.")
        query.add_prompt("Password: ", False)
        query.add_prompt("Verification code: ", True)
        return query

    def check_auth_interactive_response(self, responses):
        self.seen_responses.append(list(responses))
        if len(responses) == 2 and responses[0] == "letmein" and responses[1] == self.CODE:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args, **kwargs):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=lambda: channel.send(b"\r\nsw1#"), daemon=True).start()
        return True


def start_device() -> tuple[int, TwoFactorServer, socket.socket]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    server = TwoFactorServer()

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
            channel = transport.accept(10)
            server.keep.append((transport, channel))

    threading.Thread(target=run, daemon=True).start()
    return port, server, listener


def test_the_handler() -> None:
    print("\n-- The SSH handler --")
    port, server, listener = start_device()
    try:
        first = SSHHandler(params=ConnectionParams(
            hostname="127.0.0.1", port=port, username="neteng", password="letmein"))
        asked = None
        try:
            first.connect()
        except InteractiveRequired as exc:
            asked = exc
        except ConnectionError_ as exc:
            check("the first attempt reports the question", False, f"got a plain refusal: {exc}")
        check("the first attempt raises InteractiveRequired", asked is not None)
        if asked:
            texts = [p["text"] for p in asked.prompts]
            check("  carrying the device's prompts", texts == ["Password: ", "Verification code: "], str(texts))
            check("  and which are echoed", [p["echo"] for p in asked.prompts] == [False, True])
            check("  with the title the device gave", asked.title == "Two-factor login", asked.title)
            check("  and as a dict for the API", asked.as_dict()["prompts"][1]["text"].startswith("Verification"))
        check("the password prompt was answered from the password, the code left blank",
              server.seen_responses and server.seen_responses[-1] == ["letmein", ""],
              str(server.seen_responses))

        second = SSHHandler(params=ConnectionParams(
            hostname="127.0.0.1", port=port, username="neteng", password="letmein",
            interactive_answers=["424242"]))
        second.connect()
        check("the second attempt, with the code, connects", bool(second.is_connected))
        check("  and the answers are scrubbed afterwards", second.params.interactive_answers == [])
        second.disconnect()

        wrong = SSHHandler(params=ConnectionParams(
            hostname="127.0.0.1", port=port, username="neteng", password="letmein",
            interactive_answers=["000000"]))
        try:
            wrong.connect()
            check("a wrong code is refused", False, "connected")
        except InteractiveRequired:
            check("a wrong code is refused, not asked again", False, "asked again")
        except ConnectionError_ as exc:
            check("a wrong code is refused, not asked again", "refused the answers" in str(exc), str(exc))
    finally:
        listener.close()


def test_the_api() -> None:
    print("\n-- The API --")
    from fastapi.testclient import TestClient
    from backend.app import app

    port, server, listener = start_device()
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        first = client.post("/api/sessions", json={
            "connection_type": "ssh", "hostname": "127.0.0.1", "port": port,
            "username": "neteng", "password": "letmein", "display_label": "2fa",
        })
        check("the API answers 409 with the prompts", first.status_code == 409, f"got {first.status_code} {first.text[:120]}")
        detail = first.json().get("detail", {}) if first.status_code == 409 else {}
        prompts = (detail.get("interactive") or {}).get("prompts") or []
        check("  naming what was asked", any("Verification" in p.get("text", "") for p in prompts), str(prompts))

        second = client.post("/api/sessions", json={
            "connection_type": "ssh", "hostname": "127.0.0.1", "port": port,
            "username": "neteng", "password": "letmein", "display_label": "2fa",
            "interactive_answers": ["424242"],
        })
        check("with the answers, a session is created", second.status_code == 200, f"got {second.status_code} {second.text[:120]}")
        if second.status_code == 200:
            sid = second.json()["session_id"]
            check("  and the answers never appear in the session record",
                  "424242" not in second.text)
            client.delete(f"/api/sessions/{sid}")
    finally:
        listener.close()


def main() -> int:
    print("=" * 52)
    print("  Keyboard-interactive login")
    print("=" * 52)
    for test in (test_the_handler, test_the_api):
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
