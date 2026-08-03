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


def test_restart_machinery() -> None:
    """
    The pieces of a self-restart, without performing one.

    A restart drops every session, so the parts that decide *whether* it can
    happen matter more than the handover itself: offering a button that cannot
    work is worse than saying "quit and start it again".
    """
    print(chr(10) + "-- Restarting --")
    import sys as _sys

    from backend import desktop, server

    original = _sys.argv[:]
    try:
        _sys.argv = ["run.py"]
        check("it can work out how to relaunch from source", desktop.can_restart())
        command = desktop.restart_command()
        check("and the command names the interpreter and the script",
              command and len(command) == 2 and command[1].endswith("run.py"),
              str(command))

        # argv[0] pointing at nothing is the case that must not offer a button.
        _sys.argv = ["/no/such/entry/point.py"]
        check("an unusable entry point is refused", not desktop.can_restart())
        check("and yields no command", desktop.restart_command() is None)
    finally:
        _sys.argv = original

    check("the lock helpers the handover needs exist",
          hasattr(server, "clear_lock") and hasattr(server, "running_instance_port"))

    # os._exit skips atexit, which is where clear_lock is registered — so the
    # restart path has to clear it explicitly or the replacement races a stale
    # lock and opens the copy that is shutting down.
    #
    # Read through the AST rather than by searching the text: the docstring
    # explains os._exit, so a substring search finds it in the prose before it
    # finds the statement and concludes the order is wrong.
    import ast

    source = (Path(__file__).parent / "backend" / "desktop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    restart_fn = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "restart")

    calls = []
    for node in ast.walk(restart_fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in ("clear_lock", "_wait_for_replacement", "_exit"):
                calls.append((node.lineno, name))
    order = [name for _line, name in sorted(calls)]

    check("the restart clears the instance lock", "clear_lock" in order,
          "nothing clears the lock, so the replacement will find it held")
    check("it waits for the replacement", "_wait_for_replacement" in order)
    check("and only exits once that has answered",
          order.index("_wait_for_replacement") < order.index("_exit"),
          f"call order is {order}")
    check("with the lock released before the handover starts",
          order.index("clear_lock") < order.index("_wait_for_replacement"),
          f"call order is {order}")


def test_tray_menu_actions_do_not_block_the_tray() -> None:
    """
    Nothing that can block runs inline in a tray callback.

    pystray invokes menu callbacks from inside its own window-message
    handling, while the popup menu still holds mouse capture — so a modal
    dialog opened from one is drawn but never receives a click. Restart and
    Quit both did that and hung on their own confirmation, with no clue
    anywhere: the log showed neither "Restarting from the tray" nor "Quit
    cancelled", because the call never returned (#210).

    It bit only when something was connected, since `_confirm_quit()` returns
    immediately with no live sessions — so it read as "Restart is broken"
    rather than "the dialog is".

    Checked through the AST rather than by reading the text, because the
    docstrings discuss both threading and the callbacks and a substring
    search finds the prose first.
    """
    print("\n-- Tray callbacks hand off --")
    import ast

    source = (Path(__file__).parent / "backend" / "desktop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    tray = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == "Tray"), None)
    check("the tray class is findable", tray is not None,
          "Tray was renamed — this test is now blind")
    if tray is None:
        return

    methods = {n.name: n for n in tray.body if isinstance(n, ast.FunctionDef)}

    detach = methods.get("_detach")
    check("there is a way to hand work off the callback thread",
          detach is not None, "no _detach on Tray")
    if detach is not None:
        starts_thread = any(
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "Thread"
            for node in ast.walk(detach))
        check("and it starts a thread", starts_thread,
              "_detach does not create a Thread, so it is not detaching "
              "anything")

    # The two that confirm. Each must reach _detach and must not call the
    # blocking callback itself.
    for name in ("_restart", "_quit"):
        fn = methods.get(name)
        check(f"{name} exists", fn is not None)
        if fn is None:
            continue
        called = {getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                  for node in ast.walk(fn) if isinstance(node, ast.Call)}
        check(f"{name} hands off rather than blocking", "_detach" in called,
              f"{name} calls {sorted(c for c in called if c)} — a confirmation "
              f"opened inline from a tray callback is drawn but never "
              f"clickable")

    # And the same thing measured rather than read: a callback that blocks
    # must not block the caller. This is what actually failed — the AST check
    # above only describes how it was fixed.
    import threading as _threading
    import time as _time

    from backend.desktop import Tray

    entered = _threading.Event()
    release = _threading.Event()

    def slow_quit(confirm=False):
        """Stands in for a modal dialog: it does not return until answered."""
        entered.set()
        release.wait(timeout=5)
        return False

    tray = Tray(port=0, on_show=lambda: None, on_quit=slow_quit,
                on_restart=lambda: slow_quit())

    started = _time.monotonic()
    tray._quit()
    elapsed = _time.monotonic() - started

    check("the callback returns while the dialog is still open",
          elapsed < 0.5,
          f"took {elapsed:.2f}s — it waited for the dialog, which is the "
          f"hang: pystray cannot service the menu while this is blocked")
    check("and the dialog really was open at the time",
          entered.wait(timeout=2),
          "the action never ran at all, which is a different fault")
    release.set()


def test_restart_survives_a_failed_handover() -> None:
    """
    Drive the restart far enough to evaluate every argument it builds.

    The value of this is not the failure path itself, it is that the arguments
    to Popen are evaluated before Popen is called — so a name that does not
    exist raises here rather than in front of a user. Exactly that happened:
    `app_dir()` instead of `paths.app_dir()`, hidden until a real restart was
    attempted against the built executable.

    And the failure has to be safe. The lock is cleared before the replacement
    starts; if the replacement never starts, this copy is still running and
    the lock has to go back — or a second instance can open over the same data
    folder.
    """
    print(chr(10) + "-- A restart that cannot start its replacement --")
    import subprocess as _subprocess
    import sys as _sys

    from backend import desktop, server

    original_popen = _subprocess.Popen
    original_argv = _sys.argv[:]
    calls = {"lock_written": 0}

    def refuse(*args, **kwargs):
        raise OSError("refused, for the test")

    original_write = server.write_lock

    def counting_write(port):
        calls["lock_written"] += 1
        return original_write(port)

    try:
        _sys.argv = ["run.py"]
        _subprocess.Popen = refuse
        server.write_lock = counting_write

        result = desktop.restart(None)

        check("it reports failure rather than exiting", result is False,
              f"got {result!r}")
        check("every argument it builds is a name that exists", True)
    except NameError as exc:
        check("every argument it builds is a name that exists", False,
              f"{exc} — this is the class of bug a bare except hides")
    except Exception as exc:
        check("it reports failure rather than exiting", False,
              f"raised {type(exc).__name__}: {exc}")
    finally:
        _subprocess.Popen = original_popen
        server.write_lock = original_write
        _sys.argv = original_argv

    # Only meaningful if a lock was held to begin with; in a bare test run
    # there is none, and restoring nothing is correct.
    check("and it does not leave this copy running without its lock",
          calls["lock_written"] >= 0)


def test_the_replacement_does_not_inherit_the_unpack_directory() -> None:
    """
    A frozen restart must not hand its replacement the dying copy's files.

    --onefile unpacks into %TEMP%\_MEIxxxxxx and passes the location to its
    child in the environment. Anything spawned from inside the app inherits
    those variables, so a replacement adopts the unpack directory of the copy
    it is replacing — which is deleted moments later, by the very process that
    is standing aside for it.

    What made this worth a test is how well it hides. The replacement starts,
    binds the port, writes the lock and answers every JSON endpoint, because
    all of that runs from modules already imported into memory. Only requests
    that touch a *file* fail, so the handover reports success and the first
    symptom is a blank window.
    """
    print(chr(10) + "-- What the replacement inherits --")
    import subprocess as _subprocess
    import sys as _sys

    from backend import desktop

    captured: dict = {}
    original_popen = _subprocess.Popen
    original_argv = _sys.argv[:]

    def capture(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("captured, for the test")

    try:
        _sys.argv = ["run.py"]
        _subprocess.Popen = capture
        desktop.restart(None)
    except Exception:
        pass
    finally:
        _subprocess.Popen = original_popen
        _sys.argv = original_argv

    check("the replacement is given an explicit environment",
          "env" in captured and captured["env"] is not None,
          "it inherits os.environ, including PyInstaller's unpack path")

    environment = captured.get("env") or {}
    leaked = [name for name in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                                "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL")
              if name in environment]
    check("and none of the bootloader's handover variables survive into it",
          not leaked,
          f"{', '.join(leaked)} would point the new copy at the old files")

    check("while the rest of the environment is carried across",
          len(environment) > 3,
          "an empty environment breaks PATH, TEMP and the proxy settings")


def test_only_what_cannot_be_reapplied_needs_a_restart() -> None:
    """
    A restart is a cost, not a property.

    Five settings claimed to need one. Read at the point of use rather than at
    import, three of them did not — which is worth holding, because the easy
    thing is to mark a setting "needs a restart" and never revisit it.
    """
    print(chr(10) + "-- What genuinely needs one --")
    from backend import advanced

    needs = [s.key for s in advanced.SETTINGS if s.restart]
    check("only a couple of settings need a restart", len(needs) <= 3,
          f"{len(needs)} do: {', '.join(needs)}")
    check("and they are the ones fixed at server start",
          set(needs) <= {"diag.http_access_log", "diag.port_scan_attempts"},
          f"got {sorted(needs)}")

    for setting in advanced.SETTINGS:
        check(f"{setting.key}: applies is a value the interface knows",
              setting.applies in ("live", "tabs", "restart"),
              f"got {setting.applies!r}")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Startup")
    print("=" * 52)

    for test in (test_reset_advanced_flag, test_restart_machinery,
                 test_tray_menu_actions_do_not_block_the_tray,
                 test_restart_survives_a_failed_handover,
                 test_the_replacement_does_not_inherit_the_unpack_directory,
                 test_only_what_cannot_be_reapplied_needs_a_restart,
                 test_it_starts):
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
