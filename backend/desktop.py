"""
desktop.py — Present ShellMate as a desktop application rather than a browser tab.

The UI is still a local web page; only the frame around it changes.  That keeps
the whole architecture intact — the server is unchanged, and pointing a browser
at it still works — while giving the thing an application window, its own
taskbar entry, and a tray icon that keeps sessions alive when the window is
closed.

**A ladder, not a requirement.**  A native window needs the WebView2 runtime,
which is part of Windows 11 and present on most Windows 10 machines but not
guaranteed.  Rather than fail on the machine that lacks it, this falls back to
a chromeless Edge window and then to the plain default browser.  On a tool
whose whole point is running anywhere without installing anything, refusing to
start because a runtime is missing would be a poor trade.

**Closing the window does not end the session.**  Terminal sessions live in the
server process, so shutting the window while a device is mid-reload would drop
the connection for no reason.  The close is intercepted, the window hidden, and
the tray icon left to bring it back.  Quitting is deliberate and explicit.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request
import webbrowser

from backend import paths

logger = logging.getLogger(__name__)

from backend.version import VERSION as _VERSION

WINDOW_TITLE = f"ShellMate Portable {_VERSION}"
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 900
MIN_WIDTH = 900
MIN_HEIGHT = 560


# The live pywebview window, when there is one. None under --no-window, in a
# plain browser, or when the WebView2 runtime is missing.
_active_window = None
_active_desktop = None

# Which rung of the ladder actually won (#562).
#
# The fallbacks are deliberately silent — a missing WebView2 runtime is not a
# reason to be unable to reach a device — but silent is not the same as
# unknowable. "It opened in Chrome" is one of the two things people report
# about a copy on a stick, and until now the only record of which frame took
# it was a line in a log nobody reads. Diagnostics asks this.
_frame = "not started"


def frame_in_use() -> str:
    """The window frame this process ended up with, in the user's words."""
    return _frame


def has_native_window() -> bool:
    """True when a real OS file dialog can be raised."""
    return _active_window is not None


def pick_file(title: str = "Select a file",
              directory: str = "",
              file_types: tuple[str, ...] = ()) -> str | None:
    """
    Raise the platform's own file dialog and return the chosen path.

    Returns None when the user cancels *and* when there is no native window,
    which the caller has to distinguish by asking :func:`has_native_window`
    first — the two mean different things to the interface, which offers its
    own browser in the second case.

    pywebview marshals this onto the GUI thread itself, so calling it from the
    server thread is supported.
    """
    window = _active_window
    if window is None:
        return None
    try:
        import webview
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=directory or "",
            allow_multiple=False,
            file_types=file_types or (),
        )
    except Exception as exc:
        logger.info("Native file dialog failed (%s)", exc)
        return None

    if not result:
        return None
    return result[0] if isinstance(result, (list, tuple)) else str(result)


def pick_directory(title: str = "Select a folder", directory: str = "") -> str | None:
    """
    Raise the platform's own folder dialog and return the chosen path.

    Same contract as :func:`pick_file`: None means either "cancelled" or "no
    native window", and the caller distinguishes them with
    :func:`has_native_window` before asking.

    A separate call rather than a flag on ``pick_file`` because the underlying
    dialog is a different one — a file dialog pointed at a folder returns
    nothing when the user clicks a folder rather than opening it.
    """
    window = _active_window
    if window is None:
        return None
    try:
        import webview
        result = window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=directory or "",
        )
    except Exception as exc:
        logger.info("Native folder dialog failed (%s)", exc)
        return None

    if not result:
        return None
    return result[0] if isinstance(result, (list, tuple)) else str(result)


def wait_until_serving(port: int, timeout: float = 15.0) -> bool:
    """
    Block until the server answers, or the timeout expires.

    A native window that opens before the server is listening shows an error
    page and stays showing it — unlike a browser, there is no reload button to
    rescue the situation.
    """
    deadline = time.time() + timeout
    # /api/health, not /api/system/info: with auth enabled the info route
    # answers 401, which read as "not started" and killed the launch (#329).
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    return False


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------


def _tray_image():
    """
    The application icon, for the tray.

    Built by :mod:`backend.branding`, which is also what the executable's
    ``.ico`` is generated from — so the tray, the taskbar and the window all
    show the same mark rather than three near-misses.
    """
    from backend import branding

    return branding.app_image(branding.TRAY_SIZE)


def _window_icon():
    """
    The ``.ico`` for the window, generating it if the build did not.

    Running from source there is no build step to have produced it, and a
    developer should see the same icon a user does.
    """
    from backend import branding

    path = branding.icon_path()
    if path.exists():
        return path
    return branding.write_ico(path)


class Tray:
    """
    System tray icon.

    Runs on its own thread because both pystray and the window want a message
    loop of their own, and the window has to own the main thread.
    """

    def __init__(self, port: int, on_show, on_quit, on_restart=None):
        self.port = port
        self._on_show = on_show
        self._on_quit = on_quit
        # Restart is not a quit — it confirms, then hands over to a fresh
        # copy — so it gets its own callback rather than overloading the one
        # whose contract is "this process ends".
        self._on_restart = on_restart
        self._icon = None
        self._thread = None

    def start(self) -> bool:
        """Start the tray icon. Returns False if unavailable."""
        try:
            import pystray
        except ImportError:
            logger.info("pystray is not available; running without a tray icon")
            return False

        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open ShellMate", self._show, default=True),
                pystray.MenuItem("Open in browser", self._browser),
                pystray.Menu.SEPARATOR,
                # Restarting used to mean quitting and finding the executable
                # again — a poor answer for the action most likely to be
                # wanted after changing something (#142). Only offered when
                # it can actually be done: from source without a resolvable
                # script, can_restart() is false and a menu entry that does
                # nothing is worse than none.
                pystray.MenuItem("Restart", self._restart,
                                 visible=lambda item: self._on_restart is not None
                                                      and can_restart()),
                # Asks the running server (#441): the answer arrives as a
                # tray notification, and a newer release opens its page.
                pystray.MenuItem("Check for updates", self._check_updates),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )
            self._icon = pystray.Icon(
                "shellmate", _tray_image(), f"{WINDOW_TITLE} — port {self.port}", menu,
            )
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
            return True
        except Exception as exc:
            logger.warning("Could not create the tray icon: %s", exc)
            return False

    # pystray passes (icon, item) to every callback.
    def _show(self, icon=None, item=None):
        try:
            self._on_show()
        except Exception as exc:
            logger.warning("Could not show the window: %s", exc)

    def _browser(self, icon=None, item=None):
        webbrowser.open(f"http://localhost:{self.port}")

    def _check_updates(self, icon=None, item=None):
        """Ask our own server whether a newer release exists (#441)."""
        def run():
            message = "Could not check for updates."
            url = ""
            try:
                import httpx
                data = httpx.get(f"http://127.0.0.1:{self.port}/api/system/update",
                                 timeout=10.0).json()
                if data.get("error"):
                    message = data["error"]
                elif data.get("note"):
                    message = f"{data['note']} You are running {data.get('current', '')}."
                elif data.get("newer"):
                    message = (f"ShellMate {data.get('latest')} is available; you are running "
                               f"{data.get('current')}. Opening the release page.")
                    url = data.get("url", "")
                else:
                    message = f"You are up to date ({data.get('current', '')})."
            except Exception as exc:                      # pragma: no cover - UI
                message = f"Could not check for updates: {exc}"
            try:
                if self._icon is not None:
                    self._icon.notify(message, "ShellMate")
            except Exception:
                logger.info("Update check: %s", message)
            if url:
                webbrowser.open(url)
        threading.Thread(target=run, daemon=True, name="update-check").start()

    def _restart(self, icon=None, item=None):
        """
        Relaunch, asking first when devices are connected.

        A restart drops every session in the process, which is the thing the
        desktop design otherwise avoids doing by accident — closing the window
        only hides it precisely because sessions live here. A tray menu is one
        careless click away, so this gets the same confirmation Quit has, and
        for the same reason.

        The mechanism is the one built for the settings that need a restart:
        it strips the _PYI_* bootloader variables, without which the new copy
        inherits the dying one's unpack directory and comes up with no files,
        and it releases the listening socket first, because a Windows
        SO_REUSEADDR bind succeeds against a port that is still listening and
        the new copy came up on 8766 with nothing pointing at it.
        """
        if self._on_restart is None:
            return
        self._detach("Restart", self._on_restart)

    def _quit(self, icon=None, item=None):
        def go():
            # The confirmation has to happen before the icon is destroyed, or
            # a user who says "no" is left with a running application and no
            # tray.
            if not self._on_quit(confirm=True):
                return
            self.stop()

        self._detach("Quit", go)

    def _detach(self, what: str, action) -> None:
        """
        Run a menu action off the tray's callback thread.

        **Nothing that can block may run inline here.** pystray invokes menu
        callbacks from inside its own window-message handling, while the popup
        menu still holds mouse capture — so a modal dialog opened from one is
        drawn but never receives a click. Both Restart and Quit did exactly
        that, and hung on their own confirmation with no clue anywhere: the
        log showed neither "Restarting from the tray" nor "Quit cancelled",
        because the call never came back (#210).

        It only ever bit when something was connected. `_confirm_quit()`
        returns True immediately with no live sessions, so with nothing open
        both worked — which is why this read as "Restart is broken" rather
        than "the dialog is".

        Daemon, and nothing joins it: `quit()` ends in `os._exit(0)`, so this
        thread is the one that ends the process.
        """
        def go():
            try:
                action()
            except Exception as exc:
                logger.warning("%s failed: %s", what, exc)

        threading.Thread(target=go, daemon=True,
                         name=f"shellmate-tray-{what.lower()}").start()

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None


# ---------------------------------------------------------------------------
# Window strategies
# ---------------------------------------------------------------------------


def _find_chromium_browser() -> str | None:
    """Locate Edge or Chrome, for the chromeless-window fallback."""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which("msedge") or shutil.which("chrome")


def open_app_window(port: int) -> bool:
    """
    Open a chromeless browser window, as a fallback for a real one.

    Uses a profile directory inside the data folder so the window is not
    entangled with the user's own browser session — and so it travels with the
    executable like everything else.
    """
    browser = _find_chromium_browser()
    if not browser:
        return False

    profile = paths.data_dir() / "window-profile"
    try:
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([
            browser,
            f"--app=http://127.0.0.1:{port}",
            f"--user-data-dir={profile}",
            f"--window-size={DEFAULT_WIDTH},{DEFAULT_HEIGHT}",
            "--no-first-run",
            "--no-default-browser-check",
        ])
        logger.info("Opened a chromeless window using %s", os.path.basename(browser))
        global _frame
        _frame = f"chromeless window ({os.path.basename(browser)})"
        return True
    except Exception as exc:
        logger.info("Could not open a chromeless window: %s", exc)
        return False


class Desktop:
    """
    Owns the application window and tray icon.

    :meth:`run` blocks the main thread until the user quits, which is what the
    native GUI toolkit requires.
    """

    def __init__(self, port: int):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self._window = None
        self._tray = None
        self._quit = threading.Event()
        self._warned_about_tray = False
        # Updated as the window is dragged and resized; written on close.
        self._geometry: dict = {}

    # -- public --------------------------------------------------------------

    def run(self) -> None:
        """Show the UI and block until the user quits."""
        # The status bar's Exit (#452) comes in over the API and must end
        # everything the tray's Quit ends — so it is the same method.
        try:
            from backend import app as app_module
            app_module.register_quit_hook(self.quit)
        except Exception as exc:
            logger.warning("Could not register the quit hook: %s", exc)
        self._tray = Tray(self.port, self._show_window, self.quit,
                          on_restart=self.restart_from_tray)
        has_tray = self._tray.start()

        if self._run_native():
            return

        # No native window: fall back, then park the main thread so the tray
        # (and the server behind it) stay alive.
        if not open_app_window(self.port):
            global _frame
            _frame = "default browser"
            webbrowser.open(f"http://localhost:{self.port}")

        if not has_tray:
            logger.info("No window manager and no tray; press Ctrl+C to stop.")
        self._quit.wait()

    def restart_from_tray(self) -> bool:
        """
        Confirm, then relaunch.

        A restart drops every session in the process — the very thing the
        desktop design otherwise avoids by accident, since closing the window
        only hides it precisely because sessions live here. A tray menu is one
        careless click, so it asks the same question Quit asks, naming the
        devices that would go.

        The relaunch itself is the mechanism built for the settings that need
        one: it strips the _PYI_* bootloader variables, without which the new
        copy inherits the dying one's unpack directory and comes up with no
        files, and it releases the listening socket first, because a Windows
        SO_REUSEADDR bind succeeds against a port that is still listening —
        which is how the restarted copy once came up on 8766 with nothing
        pointing at it.
        """
        if not self._confirm_quit(action="restart"):
            logger.info("Restart cancelled — sessions are still open.")
            return False

        self._remember_geometry()
        logger.info("Restarting from the tray.")
        return restart(self)

    def quit(self, confirm: bool = False) -> bool:
        """
        Shut everything down.

        Args:
            confirm: Ask first when devices are still connected. Quitting is
                the one action that really does drop every session, which is
                exactly what closing the window was so carefully stopped from
                doing — and it used to say nothing at all.

        Returns:
            False when the user changed their mind. Otherwise it does not
            return: the process ends.
        """
        if confirm and not self._confirm_quit():
            logger.info("Quit cancelled — sessions are still open.")
            return False

        self._remember_geometry()
        logger.info("Shutting down.")
        if self._tray:
            self._tray.stop()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
        self._quit.set()
        # The server runs on a daemon thread, so ending the process is the
        # shutdown. os._exit skips interpreter teardown, which otherwise hangs
        # on the GUI thread that is still unwinding underneath us.
        os._exit(0)

    def _confirm_quit(self, action: str = "quit") -> bool:
        """
        Ask before dropping live sessions. True means go ahead.

        Deliberately a native message box rather than something in the web
        page: the tray menu is reachable when the window is hidden, and asking
        through a window nobody can see would be no question at all.
        """
        try:
            from backend.settings_store import get_settings
            if not get_settings().get("interface", {}).get("confirm_quit", True):
                return True
        except Exception:
            pass                                    # never block quitting

        try:
            from backend.app import session_manager
            live = [s for s in session_manager.get_all_sessions()
                    if s.get("is_connected")]
        except Exception:
            return True

        if not live:
            return True

        names = [s.get("display_label") or s.get("hostname") or "session"
                 for s in live]
        listed = "\n".join(f"  · {n}" for n in names[:10])
        if len(names) > 10:
            listed += f"\n  · and {len(names) - 10} more"

        # Worded for what is about to happen. A restart drops the sessions
        # just as surely as a quit, but says something different about what
        # comes next, and a dialog reading 'quit' when you pressed Restart
        # is the kind of thing that gets clicked through without reading.
        restarting = action == "restart"
        if restarting:
            tail = ("Restarting disconnects them and opens a fresh copy. "
                    "Closing the window instead leaves everything running "
                    "in the tray.\n\nRestart anyway?")
        else:
            tail = ("Quitting disconnects them. Closing the window instead "
                    "leaves everything running in the tray.\n\nQuit anyway?")

        message = (
            f"{len(names)} session{'' if len(names) == 1 else 's'} still connected:\n\n"
            f"{listed}\n\n" + tail
        )

        if sys.platform == "win32":
            try:
                import ctypes
                # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST.
                # IDYES == 6.
                #
                # TOPMOST as well as SETFOREGROUND: this is asked from the
                # tray, which is reachable with the window hidden, so there is
                # nothing to own the dialog and nothing guaranteeing it lands
                # in front of whatever the user is actually looking at. A
                # confirmation behind another window is a hang as far as
                # anybody can tell.
                flags = 0x04 | 0x30 | 0x10000 | 0x40000
                answer = ctypes.windll.user32.MessageBoxW(
                    None, message,
                    f"{WINDOW_TITLE} — {'restart' if restarting else 'quit'}?",
                    flags,
                )
                return answer == 6
            except Exception as exc:
                logger.warning("Could not ask before quitting (%s); quitting", exc)
                return True

        # Nowhere to ask. Say what is being lost rather than losing it quietly.
        logger.warning("Quitting with %s session(s) still connected: %s",
                       len(names), ", ".join(names))
        return True

    # -- window geometry -----------------------------------------------------

    def _saved_geometry(self) -> dict:
        """
        Where and how big to open, from settings.

        Clamped to the desktop that actually exists. A window last used on a
        second monitor that has since been unplugged would otherwise open at
        coordinates nobody can reach, with no way to drag it back.
        """
        geometry = {
            "width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT,
            "x": None, "y": None, "start_minimised": False,
        }

        try:
            from backend.settings_store import get_settings
            stored = get_settings().get("window", {})
        except Exception:
            return geometry

        geometry["start_minimised"] = bool(stored.get("start_minimised"))

        width, height = int(stored.get("width") or 0), int(stored.get("height") or 0)
        if width >= MIN_WIDTH and height >= MIN_HEIGHT:
            geometry["width"], geometry["height"] = width, height
        else:
            # No usable size stored: a remembered position without one would
            # place a default-sized window somewhere arbitrary.
            return geometry

        x, y = stored.get("x"), stored.get("y")
        if x is None or y is None:
            return geometry

        screen = self._screen_size()
        if screen:
            screen_w, screen_h = screen
            # At least a strip of the title bar has to remain grabbable.
            if not (-geometry["width"] + 120 < x < screen_w - 120
                    and -20 < y < screen_h - 80):
                logger.info("Stored window position is off-screen; centring instead")
                return geometry

        geometry["x"], geometry["y"] = int(x), int(y)
        return geometry

    @staticmethod
    def _screen_size() -> tuple[int, int] | None:
        """The primary display's size, or None when it cannot be determined."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN: the whole desktop across
            # every monitor, which is what a window position is relative to.
            return (user32.GetSystemMetrics(78) or user32.GetSystemMetrics(0),
                    user32.GetSystemMetrics(79) or user32.GetSystemMetrics(1))
        except Exception:
            return None

    def _on_resized(self, width, height):
        self._geometry.update({"width": int(width), "height": int(height)})

    def _on_moved(self, x, y):
        self._geometry.update({"x": int(x), "y": int(y)})

    def _remember_geometry(self) -> None:
        """
        Write the window's last size and position.

        Best effort throughout: this runs while the window is closing, and a
        settings write that fails must not stop it closing.
        """
        if not self._geometry:
            return
        try:
            from backend.settings_store import get_settings, update_settings
            if not get_settings().get("window", {}).get("remember", True):
                return
            update_settings({"window": dict(self._geometry)})
        except Exception as exc:
            logger.debug("Could not remember the window geometry: %s", exc)

    # -- native window -------------------------------------------------------

    def _run_native(self) -> bool:
        """
        Open a real window using the platform webview.

        Returns False if pywebview or its runtime is unavailable, so the caller
        can fall back.
        """
        try:
            import webview
        except ImportError:
            logger.info("pywebview is not available; falling back to a browser window")
            return False

        geometry = self._saved_geometry()

        try:
            self._window = webview.create_window(
                WINDOW_TITLE,
                self.url,
                width=geometry["width"],
                height=geometry["height"],
                x=geometry["x"],
                y=geometry["y"],
                min_size=(MIN_WIDTH, MIN_HEIGHT),
                hidden=geometry["start_minimised"],
                # The terminal depends on being able to select text.
                text_select=True,
            )
            self._window.events.closing += self._on_closing

            # Remember where it was left. These fire on every frame of a drag,
            # so the handlers only record; the write happens when the window
            # closes. Wrapped because not every pywebview build publishes both,
            # and losing the remembered geometry is not worth failing to start.
            try:
                self._window.events.resized += self._on_resized
            except Exception:
                logger.debug("Window resize events unavailable")
            try:
                self._window.events.moved += self._on_moved
            except Exception:
                logger.debug("Window move events unavailable")

            # Published so the HTTP layer can raise a real OS file dialog. The
            # UI is a web page and a browser file input yields no filesystem
            # path, only contents — and a path is exactly what SSH key
            # authentication needs.
            global _active_window, _active_desktop
            _active_window = self._window
            _active_desktop = self

            # private_mode=False with a storage path in the data directory.
            # Interface preferences now live in settings.json rather than in
            # here, but the webview still keeps its own state — scroll
            # positions, form values — and private mode discards the lot on
            # every launch.
            storage = paths.data_dir() / "window-storage"
            storage.mkdir(parents=True, exist_ok=True)

            # Stop WebView2 fetching components a terminal will never call.
            #
            # Measured before this existed: 77 MB of a 78 MB data folder was
            # WebView2's, against about 1 MB of actual settings, vault,
            # history and snippets. 22 MB of it was Widevine — DRM for
            # playing protected video — 12 MB ad-blocking filter lists, and
            # 2.6 MB speech recognition.
            #
            # Two reasons it matters here and would not in an installed app.
            # ShellMate-Data is the folder that *travels*: carrying 77 MB of
            # somebody else's cache on a stick to hold 1 MB of settings is
            # the opposite of portable. And these are downloaded, so a build
            # described as working air-gapped spent its first launch reaching
            # out to Microsoft for a DRM module — "no CDN in the frontend"
            # was never the whole of that claim.
            #
            # Set as an environment variable because that is the only hook
            # the WebView2 loader offers; it is read when the environment is
            # created, so it must be set before the window is.
            #
            # GrShaderCache and the profile itself are left alone. The shader
            # cache is what stops the terminal recompiling shaders on every
            # launch, and a cache that rebuilds each time is worse than one
            # that persists.
            os.environ.setdefault(
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                "--disable-features=msWidevineCdm,SubresourceFilter,"
                "OptimizationGuideModelDownloading,MediaFoundationClearPlayback "
                "--disable-component-update "
                "--disable-speech-api",
            )

            # On Windows the window and taskbar icons come from the
            # executable, which build.spec now sets — so this only takes
            # effect under GTK/Qt, where pywebview honours it. Passed anyway
            # rather than branching: the point of one icon in three places is
            # that nothing has to remember which platform reads which.
            start_args = {"private_mode": False, "storage_path": str(storage)}
            icon = _window_icon()
            if icon:
                start_args["icon"] = str(icon)

            global _frame
            _frame = "native window"
            try:
                webview.start(**start_args)
            except TypeError:
                # An older pywebview without the icon argument. Losing the
                # icon is not a reason to lose the window.
                start_args.pop("icon", None)
                webview.start(**start_args)
            return True

        except Exception as exc:
            # Most likely the WebView2 runtime is missing on an older machine.
            logger.info("Native window unavailable (%s); falling back", exc)
            self._window = None
            _frame = "not started"
            return False

    def _on_closing(self) -> bool:
        """
        Intercept the window close: hide instead of exiting.

        Terminal sessions live in the server process. Closing the window while
        a device is mid-reload should not drop the connection, so the window
        goes away and everything behind it keeps running.

        Returning False cancels the close.
        """
        # Written here rather than on quit: quit is os._exit, which runs no
        # further Python, and closing the window is the moment the user has
        # finished arranging it anyway.
        self._remember_geometry()

        try:
            self._window.hide()
        except Exception:
            return True          # cannot hide it, so let it close properly

        if not self._warned_about_tray:
            self._warned_about_tray = True
            logger.info(
                "Window closed — ShellMate is still running and your sessions "
                "are still open. Use the tray icon to reopen it, or Quit to stop."
            )
        return False

    def _show_window(self) -> None:
        """Bring the window back, from the tray."""
        if self._window is not None:
            self._window.show()
        else:
            webbrowser.open(f"http://localhost:{self.port}")


def reveal(folder) -> bool:
    """
    Open a folder in the platform's file manager.

    Used after writing a support bundle: telling somebody a path is not the
    same as putting the file in front of them, and the whole point of the
    bundle is that it gets attached to an email.

    Returns False rather than raising when there is no way to do it — a
    headless run, or a platform without a handler. The path is shown either
    way, so this is a convenience and never the only route.
    """
    import subprocess
    from pathlib import Path

    target = Path(folder)
    try:
        if sys.platform == "win32":
            # os.startfile is the reliable one on Windows; explorer.exe
            # returns a non-zero exit code even on success.
            os.startfile(str(target))          # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
        return True
    except Exception as exc:
        logger.info("Could not open %s (%s)", target, exc)
        return False


def active_desktop():
    """The running Desktop instance, if there is one."""
    return _active_desktop


def notify(message: str, title: str = "ShellMate") -> bool:
    """
    Raise a native notification through the tray icon (#521).

    The one channel that reaches somebody with the window hidden, which is
    where ShellMate spends most of a long change — a watch rule that fires
    while the window is behind everything else has nowhere else to go.

    Best-effort by design: no tray (a browser fallback, a server deployment,
    a platform with no notification area) means False and nothing else. The
    toast in the page is still there when the window comes back.
    """
    tray = getattr(_active_desktop, "_tray", None)
    icon = getattr(tray, "_icon", None)
    if icon is None:
        return False
    try:
        # Truncated: a notification is a line, and some platforms silently
        # drop one that is longer than they will draw.
        icon.notify(str(message)[:200], title)
        return True
    except Exception as exc:
        logger.info("Could not raise a tray notification: %s", exc)
        return False


def can_restart() -> bool:
    """
    Whether ShellMate can relaunch itself.

    Frozen, ``sys.executable`` is the application. From source it is the
    interpreter and the script has to be named alongside it. If neither can be
    worked out, the interface says "quit and start it again" rather than
    offering a button that does nothing.
    """
    try:
        if getattr(sys, "frozen", False):
            return bool(sys.executable) and Path(sys.executable).exists()
        entry = Path(sys.argv[0]) if sys.argv else None
        return bool(entry and entry.exists() and Path(sys.executable).exists())
    except Exception:
        return False


def restart_command() -> list[str] | None:
    """The argv that starts a fresh copy, or None if it cannot be built."""
    if not can_restart():
        return None
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(sys.argv[0]).resolve())]


def restart(desktop=None) -> bool:
    """
    Start a fresh copy and stop this one.

    Three things have to happen in this order, and each of them is a way this
    goes wrong if it does not:

    **Release the single-instance lock first.**  ``Desktop.quit()`` ends the
    process with ``os._exit(0)``, which skips ``atexit`` — and ``clear_lock``
    is registered there.  A relaunch racing its own stale lock finds
    "ShellMate is already running" and opens the copy that is shutting down.

    **Hand off before exiting.**  The new process is confirmed to be listening
    before this one goes.  Exiting first and hoping leaves somebody with a
    window that vanished and nothing running.

    **Detach it.**  A child that dies with its parent is not a restart.

    Returns False when the handover failed, in which case this process is
    still running and the caller should say so.
    """
    from backend import server

    command = restart_command()
    if command is None:
        logger.info("Restart requested but the command could not be determined.")
        return False

    # The window's geometry is worth carrying across; the lock is not.
    if desktop is not None:
        try:
            desktop._remember_geometry()
        except Exception:
            pass

    # Remembered so it can be put back if the handover fails below. Clearing
    # it and then not restarting would leave this copy running with no lock,
    # and a second instance could start alongside it over the same data.
    held_port = server.running_instance_port()
    try:
        server.clear_lock()
    except Exception as exc:
        logger.warning("Could not clear the instance lock before restarting: %s", exc)

    def keep_running(reason: str) -> bool:
        """Abandon the restart and put things back as they were."""
        logger.warning("Restart abandoned: %s", reason)
        if held_port:
            try:
                server.write_lock(held_port)
            except Exception:
                pass
            # If the listener was already released, take the port back.
            # Otherwise this copy survives the failed restart holding every
            # open session and answering nothing at all.
            if not server.port_is_free("127.0.0.1", held_port):
                return False
            server.resume_serving(held_port)
        return False

    # Release the port before the replacement starts, and tell it which one to
    # take. Both halves are needed: a replacement that starts while this copy
    # is still listening scans past 8765 and comes up on 8766, and the window
    # the user is looking at is pointed at 8765.
    #
    # Skipped when this process has no server of its own to stand down — under
    # a test, or embedded — where there is nothing holding the port and the
    # replacement can simply take it.
    if held_port and server.is_serving():
        if not server.stop_serving("127.0.0.1", held_port):
            return keep_running(
                f"port {held_port} was not released, so a replacement would "
                f"come up on a different address")
    if held_port:
        command = command + ["--restart-port", str(held_port)]

    # Strip PyInstaller's bootloader handshake out of the environment.
    #
    # A --onefile build unpacks itself into %TEMP%\_MEIxxxxxx and tells its
    # real child where that is through these variables. They are inherited by
    # anything *we* spawn, so a replacement launched from inside the frozen
    # app reads them, concludes it is already unpacked, and adopts the
    # directory belonging to the copy that is about to exit — which the
    # outgoing bootloader then deletes.
    #
    # The result is a process that starts, listens, and answers every JSON
    # endpoint (the Python is already imported) while every file on disk has
    # gone: `GET /` returns 500 because frontend/index.html no longer exists.
    # Nothing in the handover looks wrong, because by every check we make it
    # worked.
    environment = os.environ.copy()
    for variable in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                     "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
        environment.pop(variable, None)

    creation = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — the new copy must
        # outlive this one.
        creation = 0x00000008 | 0x00000200

    try:
        subprocess.Popen(
            command,
            cwd=str(paths.app_dir()),
            env=environment,
            close_fds=True,
            creationflags=creation,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return keep_running(f"could not start a replacement process ({exc})")

    logger.info("Replacement started; waiting for it to answer before exiting.")
    if not _wait_for_replacement():
        return keep_running("the replacement never answered")

    logger.info("Handover complete. Shutting this copy down.")
    if desktop is not None:
        try:
            if desktop._tray:
                desktop._tray.stop()
            if desktop._window is not None:
                desktop._window.destroy()
        except Exception:
            pass

    os._exit(0)


def _wait_for_replacement(timeout: float = 30.0) -> bool:
    """Wait for a new instance to take the lock and answer."""
    from backend import server

    deadline = time.time() + timeout
    while time.time() < deadline:
        port = server.running_instance_port()
        if port:
            return True
        time.sleep(0.4)
    return False
