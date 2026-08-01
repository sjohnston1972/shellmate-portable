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


def main() -> int:
    print("\n" + "=" * 52)
    print("  Security")
    print("=" * 52)

    for test in (test_a_visited_page_cannot_open_a_websocket,
                 test_our_own_pages_still_work,
                 test_one_definition_of_an_allowed_origin):
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
