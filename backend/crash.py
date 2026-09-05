"""
crash.py — What ShellMate can say about a fault it did not survive (#568).

A crash on a locked-down machine produces nothing. The window closes, or a
message box points at a log file, and whether anything is ever learned from
it depends on somebody finding that file, reading it, and typing what it
said into a form. Most of the time nobody does, and the fault that took the
application down is a fault nobody has ever seen.

The relay already exists (#370), built precisely for people who cannot reach
GitHub. This is the missing half: something for it to carry.

Three rules, and the first two are the whole reason this is a module rather
than four lines in ``run.py``:

**Nothing leaves without the user reading it.** A crash file is written
automatically; sending it is a decision somebody takes with the text in
front of them. ``feedback.report_crashes`` defaults to not prompting at all.
Nothing about a fault justifies sending something the user has not seen.

**Everything goes through the one door.** A traceback embeds hostnames in
its exception text — ``ConnectionError_("could not reach core-sw-01.acme.
internal")`` is an ordinary line to write and a customer's estate to
publish. So the whole file goes through ``outbound.redact_text`` before it
is written, not before it is sent: a file on disk is a file that can be
copied out by hand.

**Never the scrollback.** The log says what ShellMate did. The session
buffer says what the device said, and that is the user's data, not
diagnostic material. The log tail is capped; the buffer is not included at
any size.

The hook is installed once, from ``run.py``, and covers threads as well —
which matters more here than in most applications, because the session read
loops, the scheduler and the store writer all live on threads, and an
exception on one of those is invisible today.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

from backend import paths

logger = logging.getLogger(__name__)

#: How many lines of the application log travel with a crash.
#:
#: Fifty rather than the whole file. What matters is the run-up to the
#: fault, and a megabyte of log is a report nobody reads and a relay
#: request that may not arrive.
LOG_TAIL_LINES = 50

#: The ceiling on the whole file, matching ``feedback.MAX_DESCRIPTION``.
#: A recursion error produces a traceback thousands of frames deep, and the
#: first twenty frames are the ones that say anything.
MAX_CHARS = 5000

#: Crash files kept on disk. Older ones are pruned, newest first — a data
#: folder that fills with them is its own fault report.
MAX_KEPT = 10

_PREFIX = "crash-"
_SUFFIX = ".json"

#: Set once the hook is installed, so a second call is a no-op rather than
#: a second layer of wrapping around the first.
_installed = False


# ---------------------------------------------------------------------------
# Writing one
# ---------------------------------------------------------------------------

def crash_dir() -> Path:
    """
    Where crash files live: the data folder, beside everything else.

    Not a subfolder. There are at most ten of them, they are named
    distinctly, and a folder somebody has to know about is a folder nobody
    looks in.
    """
    return paths.data_dir()


def _redact(text: str) -> str:
    """
    Everything through the one door, and never a failure of its own.

    If redaction itself raises — and it is a regex over text of unknown
    shape, called from an exception handler — the answer is to write
    nothing rather than to write the unredacted version. A crash report is
    worth having; it is not worth more than the guarantee.
    """
    try:
        from backend.session.outbound import redact_text
        return redact_text(text)
    except Exception:
        return "(this text could not be redacted, so it was not kept)"


def _log_tail() -> str:
    """The last lines of the application log, or a note saying why not."""
    try:
        path = paths.data_dir() / "shellmate.log"
        if not path.exists():
            return "(no log file for this run)"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_TAIL_LINES:])
    except Exception as exc:
        return f"(the log could not be read: {exc})"


def _about() -> str:
    """
    The About section — version, frozen or not, where the data lives.

    Reused from ``support.py`` rather than rebuilt. The first question
    asked of any crash report is which build it came from, and two places
    computing that answer is two places for it to be subtly different.
    """
    try:
        from backend.support import _about as about_section
        return about_section({})
    except Exception as exc:
        return f"(the About section could not be built: {exc})"


def write(exc_type, exc_value, exc_tb, where: str = "main") -> Path | None:
    """
    Record one fault. Returns the file written, or None.

    Never raises. It is called from an exception handler and from
    ``run.py``'s startup failure path, and a crash reporter that crashes
    replaces a diagnosable fault with an undiagnosable one.

    Args:
        exc_type, exc_value, exc_tb: as ``sys.excepthook`` receives them.
        where: what was running — "main", "thread: ollama-pull", "startup".
            Named rather than inferred, because the thread that died is
            frequently the only clue as to what it was doing.
    """
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))

        report = {
            "when":      time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "where":     where,
            # The exception's own str() is the single most useful line and
            # the single most likely to name a host, so it is redacted like
            # everything else rather than trusted for being short.
            "exception": _redact(f"{getattr(exc_type, '__name__', exc_type)}: "
                                 f"{exc_value}")[:400],
            "traceback": _redact(body)[:MAX_CHARS],
            "about":     _redact(_about()),
            "log":       _redact(_log_tail())[:MAX_CHARS],
        }

        path = crash_dir() / f"{_PREFIX}{stamp}{_SUFFIX}"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        prune()
        logger.error("Recorded a crash in %s (%s)", path.name, where)
        return path
    except Exception as exc:            # pragma: no cover - the last resort
        try:
            logger.error("Could not record the crash: %s", exc)
        except Exception:
            pass
        return None


def prune(keep: int = MAX_KEPT) -> int:
    """Delete all but the newest *keep* crash files. Returns how many went."""
    removed = 0
    try:
        files = sorted(crash_dir().glob(f"{_PREFIX}*{_SUFFIX}"),
                       key=lambda p: p.name, reverse=True)
        for path in files[keep:]:
            path.unlink(missing_ok=True)
            removed += 1
    except Exception as exc:
        logger.debug("Could not prune crash files: %s", exc)
    return removed


# ---------------------------------------------------------------------------
# What the next launch asks about
# ---------------------------------------------------------------------------

def pending() -> list[dict]:
    """
    Crash files not yet dealt with, newest first.

    Each carries its ``file`` name, so the panel can send or discard one by
    name rather than by index into a list that may have changed underneath
    it.
    """
    out: list[dict] = []
    try:
        files = sorted(crash_dir().glob(f"{_PREFIX}*{_SUFFIX}"),
                       key=lambda p: p.name, reverse=True)
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out.append({**data, "file": path.name})
            except (OSError, ValueError):
                # A half-written file from a fault during the fault. Not
                # worth reporting and not worth keeping.
                path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("Could not list crash files: %s", exc)
    return out


def get(name: str) -> dict | None:
    """One crash file by name, or None. Refuses anything but a plain name."""
    if not _is_crash_name(name):
        return None
    try:
        data = json.loads((crash_dir() / name).read_text(encoding="utf-8"))
        return {**data, "file": name} if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def discard(name: str) -> bool:
    """Forget one crash file. True if there was one to forget."""
    if not _is_crash_name(name):
        return False
    path = crash_dir() / name
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _is_crash_name(name: str) -> bool:
    """
    A plain crash file name and nothing else.

    The name arrives from the browser, and ``discard`` deletes what it is
    given. A path separator or a ``..`` here is a delete-anything endpoint,
    which is worth one line to make impossible rather than to review.
    """
    name = str(name or "")
    return (name.startswith(_PREFIX) and name.endswith(_SUFFIX)
            and "/" not in name and "\\" not in name and ".." not in name)


def as_description(report: dict) -> str:
    """
    A crash file as the body of a feedback report.

    This is the text the user is shown before anything is sent, and it is
    the text that is sent — the same string, not two renderings of it.
    Somebody who reads a preview and then finds something else went is
    somebody who will never use the preview again.
    """
    return "\n".join([
        f"ShellMate faulted in: {report.get('where', '?')}",
        f"When: {report.get('when', '?')}",
        "",
        report.get("exception", "") or "(no exception line)",
        "",
        "--- Traceback ---",
        report.get("traceback", "") or "(none)",
        "",
        "--- About ---",
        report.get("about", "") or "(none)",
        "",
        f"--- Application log, last {LOG_TAIL_LINES} lines ---",
        report.get("log", "") or "(none)",
    ])


def title_for(report: dict) -> str:
    """A one-line title: the exception, trimmed."""
    line = (report.get("exception") or "A fault with no exception line")
    return f"Crash: {line.splitlines()[0][:150]}"


# ---------------------------------------------------------------------------
# The hooks
# ---------------------------------------------------------------------------

def install() -> None:
    """
    Catch what nothing else catches, on the main thread and on threads.

    Threads matter more here than in most applications: the session read
    loops, the scheduler and the store writer all run on them, and an
    exception on one of those currently disappears into a log line at best.

    The previous hooks are called afterwards rather than replaced. The
    default one prints the traceback, which is how anybody running from
    source sees a fault at all, and taking that away to gain a JSON file
    would be a poor trade.
    """
    global _installed
    if _installed:
        return
    _installed = True

    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        # KeyboardInterrupt is somebody pressing Ctrl-C. It is not a fault
        # and a report about it is noise in an outbox.
        if not issubclass(exc_type, KeyboardInterrupt):
            write(exc_type, exc_value, exc_tb, "main")
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook

    previous_thread = getattr(threading, "excepthook", None)

    def thread_hook(args):
        if not issubclass(args.exc_type, SystemExit):
            name = getattr(args.thread, "name", "?")
            write(args.exc_type, args.exc_value, args.exc_traceback,
                  f"thread: {name}")
        if previous_thread is not None:
            previous_thread(args)

    if previous_thread is not None:
        threading.excepthook = thread_hook

    logger.debug("Crash reporting installed")
