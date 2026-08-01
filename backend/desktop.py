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
import urllib.error
import urllib.request
import webbrowser

from backend import paths

logger = logging.getLogger(__name__)

WINDOW_TITLE = "ShellMate Portable"
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 900
MIN_WIDTH = 900
MIN_HEIGHT = 560


# The live pywebview window, when there is one. None under --no-window, in a
# plain browser, or when the WebView2 runtime is missing.
_active_window = None


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
    url = f"http://127.0.0.1:{port}/api/system/info"
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

    def __init__(self, port: int, on_show, on_quit):
        self.port = port
        self._on_show = on_show
        self._on_quit = on_quit
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

    def _quit(self, icon=None, item=None):
        # The confirmation has to happen before the icon is destroyed, or a
        # user who says "no" is left with a running application and no tray.
        if not self._on_quit(confirm=True):
            return
        self.stop()

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
        self._tray = Tray(self.port, self._show_window, self.quit)
        has_tray = self._tray.start()

        if self._run_native():
            return

        # No native window: fall back, then park the main thread so the tray
        # (and the server behind it) stay alive.
        if not open_app_window(self.port):
            webbrowser.open(f"http://localhost:{self.port}")

        if not has_tray:
            logger.info("No window manager and no tray; press Ctrl+C to stop.")
        self._quit.wait()

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

    def _confirm_quit(self) -> bool:
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

        message = (
            f"{len(names)} session{'' if len(names) == 1 else 's'} still connected:\n\n"
            f"{listed}\n\n"
            "Quitting disconnects them. Closing the window instead leaves "
            "everything running in the tray.\n\nQuit anyway?"
        )

        if sys.platform == "win32":
            try:
                import ctypes
                # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND; IDYES == 6.
                answer = ctypes.windll.user32.MessageBoxW(
                    None, message, f"{WINDOW_TITLE} — quit?", 0x04 | 0x30 | 0x10000,
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
            global _active_window
            _active_window = self._window

            # private_mode=False with a storage path in the data directory.
            # Interface preferences now live in settings.json rather than in
            # here, but the webview still keeps its own state — scroll
            # positions, form values — and private mode discards the lot on
            # every launch.
            storage = paths.data_dir() / "window-storage"
            storage.mkdir(parents=True, exist_ok=True)

            # On Windows the window and taskbar icons come from the
            # executable, which build.spec now sets — so this only takes
            # effect under GTK/Qt, where pywebview honours it. Passed anyway
            # rather than branching: the point of one icon in three places is
            # that nothing has to remember which platform reads which.
            start_args = {"private_mode": False, "storage_path": str(storage)}
            icon = _window_icon()
            if icon:
                start_args["icon"] = str(icon)

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
