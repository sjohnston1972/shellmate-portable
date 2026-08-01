# build.spec — PyInstaller build definition for ShellMate.
#
# Build with:
#     pip install -r requirements-dev.txt
#     pyinstaller build.spec --noconfirm
#
# Output: dist/ShellMate.exe — a single self-contained executable that needs
# no Python, no installer and no administrator rights.
#
# Switching to a folder build:
#     Set ONEFILE = False below. That produces dist/ShellMate/ instead, which
#     starts instantly and is far less likely to be quarantined by corporate
#     antivirus. Worth doing if the single file gets blocked in the field.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# ---------------------------------------------------------------------------
# Build shape
# ---------------------------------------------------------------------------

ONEFILE = True
APP_NAME = "ShellMate-Portable"

# ---------------------------------------------------------------------------
# Application icon
#
# Generated here, before Analysis collects frontend/, from the same source and
# the same code the tray icon uses at runtime — so the tray, the taskbar and
# the window show one mark instead of three near-misses. A checked-in .ico
# beside a checked-in .png is two files someone has to remember to regenerate
# together, which is how they drift apart.
#
# Never fatal: an icon that cannot be generated produces a build with the
# default one, not no build.
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(SPECPATH).resolve()))

try:
    from backend import branding
    ICON = branding.write_ico()
    ICON = str(ICON) if ICON else None
except Exception as exc:                          # pragma: no cover - build only
    print(f"build.spec: could not generate the application icon ({exc})")
    ICON = None

# ---------------------------------------------------------------------------
# Bundled data — everything the frontend serves at runtime.
#
# These land in the read-only resource directory (sys._MEIPASS when frozen),
# which backend/paths.py resolves via resource_dir(). User data never goes
# here; it goes to data_dir() alongside the executable.
# ---------------------------------------------------------------------------

datas = [
    ("frontend", "frontend"),
]

# ---------------------------------------------------------------------------
# Hidden imports
#
# PyInstaller follows static imports, so anything resolved by string at
# runtime has to be declared. uvicorn loads its protocol and lifespan
# implementations that way, and the backend imports several AI clients lazily
# inside request handlers.
# ---------------------------------------------------------------------------

hiddenimports = [
    *collect_submodules("uvicorn"),
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "websockets",
    "websockets.legacy",
    # Imported inside functions in backend/app.py, so not statically visible.
    "backend.ai.summarize",
    "backend.jira_client",
    # paramiko selects cipher backends dynamically.
    "paramiko",
    "cryptography",
    # pyserial picks its port-enumeration backend at runtime based on
    # sys.platform, so PyInstaller's static analysis never sees it. Without
    # these the serial port picker silently comes back empty in a frozen
    # build while working fine from source.
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.tools.list_ports_posix",
    "serial.tools.list_ports_linux",
    "serial.tools.list_ports_osx",
    "serial.win32",
    # FastAPI resolves the multipart parser by name when a route declares an
    # UploadFile (the SFTP upload endpoint).
    "multipart",
    "python_multipart",
    # The vault's master-password backend. cryptography loads its AEAD
    # implementations through a Rust extension that static analysis does not
    # follow, so name them explicitly.
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.bindings._rust",
    # The desktop window and tray icon. pywebview picks its GUI backend at
    # runtime by trying imports, so the Windows one is never statically visible.
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
]

# ---------------------------------------------------------------------------
# Excludes — large libraries nothing here uses. Without these, PyInstaller
# pulls in anything importable from the build environment and the executable
# grows by tens of megabytes.
# ---------------------------------------------------------------------------

excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "PyQt5",
    "PySide2",
    "notebook",
    "IPython",
    "pytest",
    "fontTools",   # build-time only, used by tools/vendor_assets.py
    "uharfbuzz",   # build-time only, used by tools/vendor_assets.py
]


a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)


if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        # UPX is disabled deliberately: compressed sections are one of the
        # strongest heuristic signals for antivirus, and this executable is
        # meant to be run on managed corporate machines.
        upx=False,
        runtime_tmpdir=None,
        # No console window: this is a desktop application with its own
        # window and tray icon, and a black box alongside it undermines that.
        # Nothing is lost — every run writes ShellMate-Data/shellmate.log, and
        # a startup failure raises a message box pointing at it.
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        # What Explorer, the taskbar and the window title bar all read from.
        icon=ICON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
