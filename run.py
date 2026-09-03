"""
run.py — Entry point for ShellMate.

Resolves the portable data directory, migrates data from the pre-portable
layout, refuses to start a second copy on top of a running one, picks a free
port, starts the server, then opens the application window.

**The server runs on a background thread and the window owns the main one.**
That is not a preference: native GUI toolkits require their event loop to be
on the process's main thread, so the ordering is forced. The server is a
daemon thread, so quitting the window ends the process without needing to
unwind uvicorn by hand.

This module is also the PyInstaller entry point, so it must not assume a
console is attached or that the working directory is anything in particular.
"""

import atexit
import logging
import multiprocessing
import sys
import threading
import webbrowser

import uvicorn
from dotenv import load_dotenv

from backend import auth, desktop, paths, server
from backend.vault import vault

# Load .env from beside the executable (not the working directory) before
# importing config, so os.environ is populated when its constants are read.
load_dotenv(dotenv_path=paths.env_file())

from backend.config import HOST, PORT  # noqa: E402 — must come after load_dotenv

# Import the FastAPI app object rather than passing uvicorn the import string
# "backend.app:app". A frozen build has no importable module tree on disk for
# uvicorn to resolve that string against, so the string form fails with
# "Could not import module". The string is only needed for reload/workers,
# neither of which ShellMate uses.
from backend.app import app as fastapi_app  # noqa: E402

def _configure_logging() -> None:
    """
    Log to the console when there is one, and always to a file.

    The windowed build has no console at all, so without a log file a failure
    before the window opens would leave nothing to look at. The file lives in
    the data directory, so it travels with the executable like everything else.
    """
    handlers: list[logging.Handler] = []

    try:
        log_file = paths.data_dir() / "shellmate.log"
        # Truncate on start rather than rotating: what matters is the current
        # run, and an unbounded log on a USB stick is its own problem.
        handlers.append(logging.FileHandler(log_file, mode="w", encoding="utf-8"))
    except OSError:
        pass

    # sys.stderr is None in a windowed PyInstaller build.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


_configure_logging()
logger = logging.getLogger("shellmate")


def _fatal(message: str) -> None:
    """
    Report a startup failure the user can actually see.

    With no console and no window yet, a traceback goes nowhere, so put it in
    front of them and point at the log.
    """
    logger.error("%s", message)
    if sys.platform == "win32":
        try:
            import ctypes

            log_file = paths.data_dir() / "shellmate.log"
            ctypes.windll.user32.MessageBoxW(
                None,
                f"{message}\n\nDetails are in:\n{log_file}",
                "ShellMate Portable",
                0x10,   # MB_ICONERROR
            )
        except Exception:
            pass


def _serve(port: int) -> None:
    """
    Run the web server. Called on a daemon thread.

    log_config=None stops uvicorn reconfiguring logging for the whole process.
    Its default config calls dictConfig, which replaces the handlers set up
    above — and in a windowed build that silently truncates the log file that
    is the only diagnostic there is. With it disabled, uvicorn's own output
    flows into our handlers instead.
    """
    # Every request the interface makes to its own API. Very noisy, and
    # exactly what is wanted when something in the interface is not reaching
    # the backend.
    from backend.advanced import get as advanced

    # uvicorn.run() builds and runs a Server in one call and returns no handle
    # to it. The restart path needs one: it has to stop listening before the
    # replacement can take the port.
    config = uvicorn.Config(fastapi_app, host=HOST, port=port, log_config=None,
                            log_level="info",
                            access_log=bool(advanced("diag.http_access_log")))
    instance = uvicorn.Server(config)
    server.register_server(instance, _serve)
    instance.run()


def main() -> int:
    """
    Start ShellMate.  Returns a process exit code.
    """
    # If a copy is already running, surface that one instead of starting a
    # second server fighting over the same data directory.
    existing = server.running_instance_port()
    if existing is not None:
        logger.info("ShellMate is already running on port %s - opening that instead.", existing)
        webbrowser.open(f"http://localhost:{existing}")
        return 0

    _apply_log_level()

    from backend import version as app_version  # noqa: E402

    logger.info("%s", app_version.describe())

    # A `.old.exe` beside us means the last update worked (#444).
    from backend import updater as updater_module  # noqa: E402

    removed = updater_module.tidy_after_launch()
    if removed:
        logger.info("Update complete; removed %s", removed)

    # A renewal made in the portal reaches this copy on its own (#446).
    from backend import licence as licence_module  # noqa: E402

    licence_module.start_background_refresh()

    logger.info("Data directory: %s", paths.data_dir())
    if paths.data_dir_is_fallback():
        logger.warning(
            "The folder next to the executable is not writable, so per-user "
            "storage is being used instead. Settings and profiles will not "
            "travel with the executable."
        )

    for item in paths.migrate_legacy_data():
        logger.info("Migrated %s", item)

    # The way back from an advanced setting that made ShellMate unusable. It
    # has to work without the interface, because the interface is what might
    # be broken — a reset that lives only inside the thing you cannot reach is
    # not a reset.
    if "--reset-advanced" in sys.argv:
        from backend.advanced import reset as reset_advanced

        reset_advanced()
        logger.info("Every advanced (Stockton) setting is back to its default.")

    # Write the platform definitions out now rather than on first connect, so
    # they are there to be found and edited before anyone needs them.
    from backend.platforms import load_profiles  # noqa: E402

    logger.info("Known platforms: %s", ", ".join(sorted(load_profiles())))

    # The tray, the taskbar, the window and the browser tab all show the same
    # mark. A frozen build has it already, generated by build.spec; running
    # from source there has been no build step, so make it now — before the
    # server starts serving the page that asks for it as a favicon.
    from backend import branding  # noqa: E402

    if not branding.icon_path().exists():
        branding.write_ico()

    # Move any plaintext API keys left by a pre-vault version into the vault.
    # A master-password vault cannot be read yet, so this is retried after the
    # user unlocks it (see the /api/vault/unlock handler).
    from backend.settings_store import migrate_plaintext_secrets  # noqa: E402

    if not vault.is_locked():
        for field in migrate_plaintext_secrets():
            logger.info("Moved %s out of settings.json and into the vault", field)
    else:
        logger.info("Vault is locked - waiting for the master password.")

    # A restart hands the port over explicitly. Without this the replacement
    # scans past the copy that is still shutting down and comes up on 8766,
    # leaving the user's window pointed at nothing.
    wanted = PORT
    if "--restart-port" in sys.argv:
        try:
            wanted = int(sys.argv[sys.argv.index("--restart-port") + 1])
            logger.info("Restart handover: waiting for port %s", wanted)
            server.wait_for_port(HOST, wanted)
        except (IndexError, ValueError):
            logger.info("--restart-port given without a usable number; ignoring.")

    # Bound wider than loopback with no token is the one configuration that
    # is refused outright. A warning in a log nobody reads is how an
    # installation ends up exposed; a hard failure is how it does not.
    refusal = auth.startup_refusal(HOST)
    if refusal:
        _fatal(refusal)
        return 1

    try:
        port = server.find_free_port(HOST, wanted)
        server.listening_port = port          # for anything that must name it (#479)
    except OSError as exc:
        _fatal(str(exc))
        return 1

    if port != PORT:
        logger.info("Port %s is in use - using %s instead.", PORT, port)

    server.write_lock(port)
    atexit.register(server.clear_lock)

    # Daemon thread: quitting the window ends the process, and uvicorn does not
    # have to be unwound cleanly to get there.
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    logger.info("ShellMate listening on http://%s:%s", HOST, port)

    # Opening a native window before the server answers shows an error page
    # with no reload button to rescue it, so wait for it to come up first.
    if not desktop.wait_until_serving(port):
        _fatal("ShellMate's server did not start. Another program may be "
               "holding the port, or a security product may have blocked it.")
        return 1

    if "--no-window" in sys.argv:
        # For headless use and automated testing: serve, but open nothing.
        logger.info("Running without a window (--no-window). Ctrl+C to stop.")
        threading.Event().wait()
        return 0

    if "--browser" in sys.argv:
        webbrowser.open(f"http://localhost:{port}")
        threading.Event().wait()
        return 0

    # Blocks on the main thread until the user quits from the tray.
    desktop.Desktop(port).run()
    return 0


def _apply_log_level() -> None:
    """
    Honour the Stockton log level.

    Read here rather than at import: a windowed build's log file is its only
    diagnostic, and DEBUG is the first thing support will ask for. Failing to
    read it must never stop the application starting, so anything unexpected
    leaves the level where it was.
    """
    try:
        from backend.advanced import get as advanced

        level = str(advanced("diag.log_level")).upper()
        if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            logging.getLogger().setLevel(getattr(logging, level))
            if level != "INFO":
                logger.info("Log level set to %s", level)
    except Exception as exc:                              # pragma: no cover
        logger.info("Could not apply the configured log level: %s", exc)


if __name__ == "__main__":
    # Required before any threading when frozen: without it, a PyInstaller
    # build that spawns a subprocess would re-run this entry point and
    # fork-bomb itself.
    multiprocessing.freeze_support()
    sys.exit(main())
