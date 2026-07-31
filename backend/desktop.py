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
    Load the application logo for the tray, or draw a fallback.

    A missing or unreadable logo must not stop the app starting, so anything
    that goes wrong here produces a plain coloured square instead.
    """
    from PIL import Image, ImageDraw

    logo = paths.frontend_dir() / "Untitled-removebg-preview.png"
    try:
        if logo.exists():
            image = Image.open(logo).convert("RGBA")
            return image.resize((64, 64), Image.LANCZOS)
    except Exception as exc:
        logger.info("Could not load the tray logo (%s); drawing a fallback", exc)

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(108, 99, 255, 255))
    draw.text((18, 20), ">_", fill=(255, 255, 255, 255))
    return image


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
        self.stop()
        self._on_quit()

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

    def quit(self) -> None:
        """Shut everything down."""
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

        try:
            self._window = webview.create_window(
                WINDOW_TITLE,
                self.url,
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                min_size=(MIN_WIDTH, MIN_HEIGHT),
                # The terminal depends on being able to select text.
                text_select=True,
            )
            self._window.events.closing += self._on_closing

            # Published so the HTTP layer can raise a real OS file dialog. The
            # UI is a web page and a browser file input yields no filesystem
            # path, only contents — and a path is exactly what SSH key
            # authentication needs.
            global _active_window
            _active_window = self._window

            # private_mode=False with a storage path in the data directory:
            # the UI keeps quick buttons, the chat pop-out position and the
            # Tshoot/Learn choice in localStorage, and the default private mode
            # would silently discard all of it on every launch.
            storage = paths.data_dir() / "window-storage"
            storage.mkdir(parents=True, exist_ok=True)

            webview.start(private_mode=False, storage_path=str(storage))
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
