"""
test_security.py — The promises the security model actually makes.

ShellMate has no authentication, and that is a deliberate decision resting on
one condition: nothing but this machine can reach the port. `NOT_EXPOSED` in
`backend/advanced.py` states it plainly — binding beyond 127.0.0.1 and having
no authentication go together.

That reasoning holds against other programs on the machine. It does **not**
hold against a web page the user merely visits, because their browser is on
this machine and will open a socket to loopback on the page's behalf. CORS
does not cover WebSockets: the browser sends an `Origin` header on the
handshake and the server is expected to reject unknown ones itself.

`/ws/chat` is the one that matters, because it needs no secret of any kind —
no session id, nothing unguessable — and `context_mode: "all"` builds a prompt
from every open session's buffer. An accepted socket there reads what is on
every screen and streams it back to whoever called.

    python test_security.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent

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


def test_a_visited_page_cannot_open_a_websocket() -> None:
    """
    The handshake is refused before it is accepted.

    Both endpoints, because the terminal one can type into a live device even
    though its id is a UUID — protected by secrecy alone is not protected.
    """
    print("\n-- Where a WebSocket may come from --")
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from backend.app import app

    client = TestClient(app)

    hostile = ("https://attacker.example", "http://attacker.example",
               "http://127.0.0.1.attacker.example", "null",
               "http://localhost.attacker.example")

    for origin in hostile:
        refused = False
        try:
            with client.websocket_connect("/ws/chat", headers={"origin": origin}):
                pass
        except WebSocketDisconnect:
            refused = True
        except Exception:
            refused = True
        check(f"/ws/chat refuses {origin}", refused,
              "an accepted socket here reads every open session")

    for origin in hostile[:2]:
        refused = False
        try:
            with client.websocket_connect("/ws/terminal/whatever",
                                          headers={"origin": origin}):
                pass
        except Exception:
            refused = True
        check(f"/ws/terminal refuses {origin}", refused,
              "an accepted socket here can type into a device")


def test_our_own_pages_still_work() -> None:
    """A check that refuses everything is not a check, it is an outage."""
    print("\n-- And our own pages still connect --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app)

    for origin in ("http://127.0.0.1:8765", "http://localhost:8765",
                   "http://127.0.0.1:8766", "http://localhost"):
        accepted = False
        try:
            with client.websocket_connect("/ws/chat", headers={"origin": origin}):
                accepted = True
        except Exception as exc:
            accepted = False
            detail = str(exc)
        check(f"/ws/chat accepts {origin}", accepted,
              "the interface itself would stop working")

    # A non-browser client sends no Origin and has no ambient authority to
    # borrow — nothing is attaching the user's loopback access on its behalf.
    accepted = False
    try:
        with client.websocket_connect("/ws/chat"):
            accepted = True
    except Exception:
        pass
    check("a client with no Origin is allowed", accepted,
          "scripts and tests would be locked out for no gain")


def test_one_definition_of_an_allowed_origin() -> None:
    """
    Two definitions of the same thing is how one of them ends up wrong.

    The CORS middleware and the WebSocket check must agree, because a
    divergence would show up as an endpoint that is reachable by one route
    and not the other — which nobody would notice until it mattered.
    """
    print("\n-- One rule, both places --")
    from backend.app import ALLOWED_ORIGIN, app

    cors = [m for m in app.user_middleware
            if "CORS" in str(m.cls)]
    check("the CORS middleware is installed", bool(cors))
    if cors:
        configured = cors[0].kwargs.get("allow_origin_regex")
        check("and shares the WebSocket check's pattern",
              configured == ALLOWED_ORIGIN.pattern,
              f"{configured!r} != {ALLOWED_ORIGIN.pattern!r}")

    check("localhost and 127.0.0.1 on any port match",
          all(ALLOWED_ORIGIN.match(o) for o in
              ("http://localhost", "http://localhost:1", "http://127.0.0.1:65535")))
    check("and a lookalike hostname does not",
          not any(ALLOWED_ORIGIN.match(o) for o in
                  ("http://127.0.0.1.evil.com", "http://localhost.evil.com",
                   "https://localhost", "http://evil.com/127.0.0.1")),
          "the pattern is anchored at both ends for this reason")


def test_approving_a_command_does_not_ship_the_reply_unasked() -> None:
    """
    Two decisions, not one.

    Clicking Send on a suggested command is a decision about the *device* —
    run this. It also meant posting whatever came back to Anthropic, OpenAI,
    xAI or DeepSeek, which is a different decision that nobody made. The
    prompt was composed in the browser from raw xterm output and arrived as an
    ordinary user message, so `outbound.redact_text()` never saw it: an
    approved `show running-config` sent the configuration, hashes and
    community strings included.
    """
    print(chr(10) + "-- Output sent after an approved command --")
    from backend import advanced
    from backend.app import _auto_analysis_prompt

    secret = "username admin privilege 15 secret 5 $1$abcd$EFGHijklMNOPqrst"
    community = "snmp-server community S3cretString RO"
    output = chr(10).join([f"interface Gi1/0/{n}" for n in range(400)]
                          + [secret, community])

    prompt = _auto_analysis_prompt({"command": "show running-config",
                                    "output": output})

    check("the password hash is masked", secret not in prompt,
          "the whole point of composing this server-side")
    check("and the community string", "S3cretString" not in prompt, prompt[-200:])
    check("the command is still named", "show running-config" in prompt)

    limit = advanced.get("ai.analyse_output_lines")
    check("the output is capped", "earlier lines not sent" in prompt,
          f"402 lines went with a {limit}-line cap")
    check("keeping the most recent, which is the useful end",
          "interface Gi1/0/399" in prompt)
    check("and saying how much was dropped",
          any(ch.isdigit() for ch in prompt.split("earlier lines")[0][-8:]),
          "a silent truncation reads as the whole output")

    check("there is a setting governing it at all",
          "ai.analyse_output" in {s.key for s in advanced.SETTINGS},
          "ai.suggest_commands governs whether commands are proposed and "
          "nothing governed whether approving one also ships the result")


def test_the_browser_is_not_the_guarantee() -> None:
    """
    A page left open must not keep sending after it is switched off.

    The browser checks the setting too, which is a convenience. This is the
    part that has to be true.
    """
    print(chr(10) + "-- Where the switch is enforced --")
    source = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")

    check("the chat socket checks the setting before composing",
          'advanced_setting("ai.analyse_output")' in source,
          "only the browser decides, so a stale page keeps sending")
    check("and the prompt is composed on this side",
          "_auto_analysis_prompt" in source)

    chat = (ROOT / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
    check("the browser no longer builds the prompt",
          "silentMsg" not in chat,
          "a prompt built in the browser cannot be redacted by the server")
    check("it sends the command and output as data",
          "auto_analysis" in chat)


def test_binding_wide_without_a_token_refuses_to_start() -> None:
    """
    The shipped Docker deployment bound 0.0.0.0 with no authentication.

    `docker-compose.yml` attached to an external network and the README
    explained that it "pairs naturally with a Cloudflare tunnel" — remote
    access, unauthenticated, to a tool that opens SSH sessions to network
    equipment, runs commands on them, browses the filesystem and holds saved
    credentials. Meanwhile `NOT_EXPOSED` recorded the opposite as settled
    policy.

    Refusing to start is the part that matters. A warning in a log nobody
    reads is how an installation ends up exposed.
    """
    print(chr(10) + "-- Binding beyond this machine --")
    import os

    from backend import auth

    original = os.environ.pop(auth.ENV_VAR, None)
    try:
        check("loopback with no token is fine",
              auth.startup_refusal("127.0.0.1") == "",
              "the case ShellMate is built for must not need configuring")
        check("and so is ::1", auth.startup_refusal("::1") == "")

        for host in ("0.0.0.0", "192.168.1.10", "::"):
            refusal = auth.startup_refusal(host)
            check(f"{host} with no token is refused", bool(refusal))
            check(f"{host}: and the message names the variable",
                  auth.ENV_VAR in refusal, refusal[:80])
            check(f"{host}: and offers the other way out",
                  "SHELLMATE_HOST=127.0.0.1" in refusal,
                  "telling somebody to set a token without mentioning they "
                  "could simply not bind wide is half an answer")

        os.environ[auth.ENV_VAR] = "a-long-random-token"
        check("with a token, binding wide is a deliberate choice",
              auth.startup_refusal("0.0.0.0") == "")
    finally:
        os.environ.pop(auth.ENV_VAR, None)
        if original is not None:
            os.environ[auth.ENV_VAR] = original

    # And the deployment that needs it says so rather than hoping.
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("the compose file requires the token",
          "SHELLMATE_AUTH_TOKEN" in compose and ":?" in compose,
          "it binds 0.0.0.0, so it must not start without one")


def test_the_token_is_never_the_cookie() -> None:
    """
    A stolen cookie proves somebody authenticated. It is not the credential.

    And it dies with the process, because the salt is per-run.
    """
    print(chr(10) + "-- What the cookie carries --")
    import os

    from backend import auth

    original = os.environ.get(auth.ENV_VAR)
    os.environ[auth.ENV_VAR] = "the-actual-secret-token"
    try:
        value = auth.cookie_value()
        check("the cookie is not the token",
              "the-actual-secret-token" not in value, value[:32])
        check("and does not contain it in any form",
              len(value) == 64 and all(c in "0123456789abcdef" for c in value),
              value[:40])

        check("the right token is accepted",
              auth.check_token("the-actual-secret-token"))
        check("a wrong one is not", not auth.check_token("nearly-right"))
        check("and neither is an empty one", not auth.check_token(""))

        check("a valid cookie is accepted", auth.check_cookie(value))
        check("a forged one is not", not auth.check_cookie("0" * 64))
    finally:
        os.environ.pop(auth.ENV_VAR, None)
        if original is not None:
            os.environ[auth.ENV_VAR] = original

    check("with no token configured, nothing is gated",
          not auth.enabled() and auth.check_cookie("") and auth.check_token(""),
          "the portable single-user case must be untouched")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Security")
    print("=" * 52)

    for test in (test_a_visited_page_cannot_open_a_websocket,
                 test_our_own_pages_still_work,
                 test_one_definition_of_an_allowed_origin,
                 test_approving_a_command_does_not_ship_the_reply_unasked,
                 test_the_browser_is_not_the_guarantee,
                 test_binding_wide_without_a_token_refuses_to_start,
                 test_the_token_is_never_the_cookie):
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
