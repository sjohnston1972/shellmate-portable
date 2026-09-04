"""
test_config_push.py — Applying configuration with a preview, and the way back.

A fake IOS device in-process keeps a running configuration: it enters and
leaves configuration mode, appends the lines it is given, honours "no", and
answers `show running-config`. Against it, the checks are the ones that
matter for a feature that writes to devices:

- the preview classes every line honestly and sends nothing
- a guardrail hit refuses the push unless forced
- apply wraps the lines in the platform's commands, echoes on the session,
  captures before and after, and returns the diff
- the restore proposal reverses what changed, as text for a person to read

    python test_config_push.py
"""

import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import paramiko

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-push-"))
paths._data_dir_cache = _TEMP

from backend import config_push                                           # noqa: E402
from backend.connections.base import ConnectionError_, ConnectionParams   # noqa: E402
from backend.connections.manager import SessionManager                    # noqa: E402
from backend.store import store                                           # noqa: E402

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


PROMPT = "sw1#"
BANNER = "Cisco IOS Software, C2960 Software\r\n"


class FakeIOS(paramiko.ServerInterface):
    """A switch with a running configuration you can change."""

    def __init__(self) -> None:
        # The OSPF key is here on purpose: a review sends the stanzas a change
        # lands in, and a running configuration carries credentials (#550).
        self.config = ["hostname sw1", "interface GigabitEthernet0/1", " description uplink",
                       " no shutdown", "interface GigabitEthernet0/2", " shutdown",
                       " ip ospf authentication-key 7 HASHHASH", "end"]
        self.typed: list[str] = []
        self.keep: list = []

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args, **kwargs):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self.serve, args=(channel,), daemon=True).start()
        return True

    def serve(self, channel) -> None:
        channel.send(BANNER.encode() + b"\r\n" + PROMPT.encode())
        buffer = b""
        mode = "exec"
        while True:
            try:
                data = channel.recv(1024)
            except Exception:
                return
            if not data:
                return
            channel.send(data)                       # echo, like a device
            buffer += data
            if b"\r" not in buffer and b"\n" not in buffer:
                continue
            lines = buffer.replace(b"\r", b"\n").split(b"\n")
            buffer = lines.pop()
            for raw in lines:
                line = raw.decode(errors="replace").strip()
                self.typed.append(line)
                channel.send(b"\r\n")
                if mode == "exec":
                    if line.startswith("show running-config"):
                        channel.send(("\r\n".join(self.config) + "\r\n").encode())
                    elif line in ("configure terminal", "conf t"):
                        mode = "config"
                    elif line == "write memory":
                        channel.send(b"[OK]\r\n")
                    channel.send((PROMPT if mode == "exec" else "sw1(config)#").encode())
                else:
                    if line == "end":
                        mode = "exec"
                        channel.send(PROMPT.encode())
                        continue
                    if line.startswith("no "):
                        target = line[3:].strip()
                        self.config = [c for c in self.config if c.strip() != target]
                    elif line and line not in ("", "!"):
                        if line not in [c.strip() for c in self.config]:
                            self.config.insert(len(self.config) - 1, line)
                    channel.send(b"sw1(config)#")


def start_device() -> tuple[int, FakeIOS, socket.socket]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    device = FakeIOS()

    def run():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=device)
            except Exception:
                continue
            device.keep.append(transport)

            def accept_all(t=transport):
                while t.is_active():
                    channel = t.accept(1)
                    if channel is not None:
                        device.keep.append(channel)
            threading.Thread(target=accept_all, daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return port, device, listener


def test_push() -> None:
    print("\n-- Preview, apply, restore --")
    from backend import advanced
    advanced.update({"capture.push_line_delay_ms": 20})
    port, device, listener = start_device()
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=port,
            username="neteng", password="x", display_label="sw1"))
        session_id = created["session_id"]
        session = manager.get_session(session_id)
        time.sleep(1.5)                              # banner, prompt, fingerprint
        session["fingerprint"] = {"platform": "ios", "name": "Cisco IOS", "confidence": 0.95, "source": "banner"}

        report = config_push.preview(session, "interface GigabitEthernet0/2\n description access\n no description uplink\nhostname sw1\n", fresh=True)
        statuses = {r["text"].strip(): r["status"] for r in report["lines"]}
        check("a new line reads as new", statuses.get("description access") == "add", str(statuses))
        check("a line already there reads as present", statuses.get("hostname sw1") == "present", str(statuses))
        check("a 'no' of something present reads as a removal", statuses.get("no description uplink") == "remove", str(statuses))
        check("the platform's commands are named", report["commands"]["enter"] == "configure terminal"
              and report["commands"]["exit"] == "end" and report["commands"]["save"] == "write memory", str(report["commands"]))
        check("the preview sent nothing", not any("description access" in t for t in device.typed))
        check("the summary reads as a sentence", "compared with" in report["summary"], report["summary"])

        held = config_push.preview(session, "reload\n")
        check("a dangerous line is flagged", held["dangerous"] == ["reload"], str(held["dangerous"]))
        check("apply refuses it unless forced",
              _raises(lambda: config_push.apply(session, "reload\n"), "guardrail"))

        result = config_push.apply(session, "interface GigabitEthernet0/2\n description access\n", save=True)
        typed = device.typed
        check("the lines went in wrapped in enter and exit",
              "configure terminal" in typed and "end" in typed and "description access" in typed
              and typed.index("configure terminal") < typed.index("description access") < typed.index("end"),
              str(typed[-8:]))
        check("save was sent when asked", "write memory" in typed and result["saved"])
        check("the device's configuration changed", "description access" in [c.strip() for c in device.config])
        check("before and after were captured", result["before_id"] and result["after_id"]
              and result["before_id"] != result["after_id"], str(result))
        check("the diff shows the change", "description access" in (result["diff"].get("diff") or ""), str(result["diff"])[:200])

        proposal = config_push.restore_proposal(session, result["before_id"])
        check("the way back is proposed as text, not applied",
              "no description access" in proposal["text"], proposal["text"])
        check("  and says it must be read", "best-effort" in proposal["note"])
        check("the device is untouched by the proposal", "description access" in [c.strip() for c in device.config])

        check("nothing to apply is refused", _raises(lambda: config_push.preview(session, "  \n"), "nothing"))
        session["fingerprint"] = {"platform": "generic", "name": "Unknown", "confidence": 0.0, "source": "none"}
        check("an unknown platform refuses rather than guessing",
              _raises(lambda: config_push.preview(session, "hostname x"), "no configuration commands"))
    finally:
        advanced.reset()
        if session_id:
            manager.destroy_session(session_id)
        listener.close()


def test_review() -> None:
    """
    #550: a second pair of eyes on the dry run, and nothing on the wire.

    The two things that matter are that the review reaches the device not at
    all, and that what it sends to a provider is masked — a running
    configuration carries keys, and the stanzas a change lands in are exactly
    where they are.
    """
    print("\n-- Review with the assistant --")
    from backend import advanced
    from backend.ai import explain

    advanced.update({"capture.push_line_delay_ms": 20})
    port, device, listener = start_device()
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=port,
            username="neteng", password="x", display_label="sw1"))
        session_id = created["session_id"]
        session = manager.get_session(session_id)
        time.sleep(1.5)
        session["fingerprint"] = {"platform": "ios", "name": "Cisco IOS",
                                  "confidence": 0.95, "source": "banner"}

        # A stored capture to review against — the review itself must never
        # take one, so it is taken here, deliberately, first.
        config_push.preview(session, "hostname sw1\n", fresh=True)
        before = list(device.typed)

        change = "interface GigabitEthernet0/2\n description access\n no shutdown\n"
        prompt = explain.push_review_prompt(session, change)

        check("the review sent nothing to the device", device.typed == before,
              str(device.typed[len(before):]))
        check("the proposed lines are classified for the model",
              "+  description access" in prompt, prompt)
        check("the stanza the change lands in is included",
              "interface GigabitEthernet0/2" in prompt and " shutdown" in prompt, prompt)
        check("the credential in that stanza does not leave the machine",
              "HASHHASH" not in prompt,
              "the running configuration went to a provider in the clear")
        check("it says nothing has been applied",
              "Nothing has been sent" in prompt, prompt[:200])
        check("and asks for the five things a review is for",
              "Blast radius" in prompt and "The way back" in prompt, prompt[-400:])

        # The cap: zero sends the change and no surrounding configuration.
        advanced.update({"ai.review_context_lines": 0})
        bare = explain.push_review_prompt(session, change)
        check("zero configuration lines sends the change and nothing round it",
              "+  description access" in bare and " ip ospf" not in bare, bare)

        held = explain.push_review_prompt(session, "reload\n")
        check("a guardrail hit is put in front of the reviewer",
              "guardrail would hold" in held and "reload" in held, held)
    finally:
        advanced.reset()
        if session_id:
            manager.destroy_session(session_id)
        listener.close()


def _raises(fn, needle: str) -> bool:
    try:
        fn()
    except Exception as exc:
        return needle.lower() in str(exc).lower()
    return False


def main() -> int:
    print("=" * 52)
    print("  Configuration push")
    print("=" * 52)
    for test in (test_push, test_review):
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
