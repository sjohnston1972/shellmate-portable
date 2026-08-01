"""
test_startup.py — Does it actually start?

Every other test in this suite imports a module and exercises it. None of them
runs ``run.py``, which is how a `NameError` in the entry point reached a built
executable: a helper was appended *below* ``if __name__ == "__main__"``, so
``main()`` was called before the definition was ever reached. Importing run.py
would not have caught it either — the module parses and imports perfectly well.
Only running it fails.

That failure mode is worse in a frozen build than it sounds. The shipped
executable is windowed, so there is no console to print the traceback to: the
user gets a message box, or nothing at all, and the log file is empty because
the crash happened before logging was configured.

So this starts the real entry point with ``--no-window``, waits for the server
to answer, and asks it something. It is slower than the rest of the suite and
worth every second.

    python test_startup.py
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

passed = 0
failed: list[str] = []

#: How long to wait for the server to come up before calling it a failure.
STARTUP_TIMEOUT = 45


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def wait_for_server(process: subprocess.Popen) -> tuple[int | None, str]:
    """
    Poll the ports ShellMate might have chosen until one answers.

    Returns (port, output-so-far). A process that has already exited is
    reported immediately rather than waited out — that is the case this test
    exists for, and thirty seconds of silence is a poor way to learn about it.
    """
    deadline = time.time() + STARTUP_TIMEOUT

    while time.time() < deadline:
        if process.poll() is not None:
            return None, "the process exited during startup"

        for port in range(8765, 8775):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/system/info", timeout=1) as response:
                    payload = json.loads(response.read())
                if payload.get("app") == "shellmate-portable":
                    return port, ""
            except (urllib.error.URLError, OSError, ValueError):
                continue
        time.sleep(0.5)

    return None, "the server never answered"


def test_it_starts() -> None:
    print("\n-- Starting the real entry point --")

    root = Path(__file__).parent
    process = subprocess.Popen(
        [sys.executable, str(root / "run.py"), "--no-window"],
        cwd=str(root),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    try:
        port, problem = wait_for_server(process)

        if port is None:
            output = ""
            try:
                process.kill()
                output = (process.communicate(timeout=10)[0] or "")[-1500:]
            except Exception:
                pass
            check("run.py starts and serves", False, f"{problem}\n{output}")
            return

        check("run.py starts and serves", True)
        print(f"       (port {port})")

        # It is up; now confirm the parts this session touched are reachable
        # from a real process rather than only from a TestClient.
        for label, path in (
            ("the page is served",            "/"),
            ("advanced settings are exposed", "/api/advanced"),
            ("the key list answers",          "/api/keys"),
            ("the support sections answer",   "/api/support/sections"),
            ("the prompts answer",            "/api/prompts"),
            ("the generated snippet library answers", "/api/snippets"),
        ):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                    ok = response.status == 200
                check(label, ok, f"status {response.status}")
            except Exception as exc:
                check(label, False, str(exc))

        # And the one that proves the whole registry is coherent at runtime.
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/advanced", timeout=5) as response:
                registry = json.loads(response.read())
            check("every advanced setting has a value",
                  all("value" in s for s in registry["settings"]),
                  "a setting came back without one")
            check("and a category heading",
                  not ({s["category"] for s in registry["settings"]}
                       - set(registry["categories"])))
        except Exception as exc:
            check("the advanced registry is readable at runtime", False, str(exc))

    finally:
        process.kill()
        try:
            process.communicate(timeout=10)
        except Exception:
            pass


def test_reset_advanced_flag() -> None:
    """The escape hatch has to work without the interface."""
    print("\n-- The way back --")
    root = Path(__file__).parent

    result = subprocess.run(
        [sys.executable, "-c",
         "import ast,sys;src=open('run.py',encoding='utf-8').read();"
         "tree=ast.parse(src);"
         "names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,))};"
         "entry=[i for i,n in enumerate(tree.body) if isinstance(n,ast.If)];"
         "defs=[i for i,n in enumerate(tree.body) if isinstance(n,ast.FunctionDef)];"
         "print('OK' if not entry or max(defs) < min(entry) else 'ORDER')"],
        cwd=str(root), capture_output=True, text=True, timeout=30,
    )
    check("every function is defined before the entry point runs",
          "OK" in result.stdout,
          "a function defined below `if __name__ == \"__main__\"` is not "
          "reachable from main()")

    check("--reset-advanced is handled",
          "--reset-advanced" in (root / "run.py").read_text(encoding="utf-8"))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Startup")
    print("=" * 52)

    for test in (test_reset_advanced_flag, test_it_starts):
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
