"""
configs.py — Configuration capture, storage and drift reporting.

Turns every login into a free change check.  On connect, the device's running
configuration is captured and compared against the last time you were there:
*"you were last here 12 days ago, 4 lines have changed."*  Nobody has to
remember to diff anything, and configuration drift stops being something you
only discover when it breaks.

The capture runs on a **second SSH channel**, opened on the transport the tab
already has (see ``SSHHandler.open_secondary_channel``).  That matters: the
alternative is typing into the user's live session, which would scroll their
screen and interleave a page of configuration with whatever they were doing.

Not every device cooperates.  Some switches cap concurrent sessions at one,
serial and telnet cannot multiplex at all, and the command differs by
platform.

A device that refuses a second channel falls back to ``LiveCapture``, which
runs the command through the session the user is already in — withheld from
their screen while it happens and stated once when it is done.  That is a
different risk from a second channel, so it is a setting
(``capture.live_fallback``) and it never starts mid-command.  Anything still
impossible produces a clear reason rather than an exception: a failed
snapshot is a missing nicety, not a broken session.

The commands used come from the device's fingerprinted platform profile
(``backend/platforms.py``), which is user-editable, so a platform ShellMate
does not yet know can be taught rather than patched.
"""

import difflib
import logging
import re
import threading
import time

from backend import config_archive
from backend.connections.base import ConnectionError_
from backend.connections.ssh_handler import SSHHandler
from backend.fingerprint import identify
from backend.platforms import get_profile
from backend.advanced import get as advanced
from backend.session.ansi import clean, strip_pager_prompts
from backend.session.transcript import match_prompt
from backend.store import store

logger = logging.getLogger(__name__)

# How long to wait for a configuration to finish printing. A big chassis
# config over a slow WAN link genuinely takes this long.
CAPTURE_TIMEOUT = 60.0

# Quiet period after the last byte before deciding the output has finished.
# Long enough to survive a pause mid-transfer, short enough not to feel stuck.
IDLE_SETTLE = 1.5

# Platform-specific commands now live in backend/platforms.py, where they
# are user-editable, rather than being duplicated here.


def session_platform(session: dict) -> str:
    """
    Return the platform this session was fingerprinted as.

    Falls back to identifying from the prompt when onboarding has not finished
    — capture can be triggered manually before the first second is up.
    """
    stored = session.get("fingerprint")
    if stored and stored.get("platform"):
        return stored["platform"]

    transcript = session.get("transcript")
    prompt = transcript.last_prompt if transcript else ""
    return identify(banner="", prompt=prompt).platform


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _read_until_idle(channel, timeout: float | None = None) -> str:
    """
    Read from a channel until it goes quiet or the timeout expires.

    There is no reliable end marker: the trailing prompt varies by platform
    and mode, and matching on one risks stopping early on a configuration that
    happens to contain something prompt-shaped. Waiting for the device to stop
    talking is cruder but does not truncate.
    """
    import socket

    from backend.advanced import get as advanced

    if timeout is None:
        timeout = advanced("capture.timeout")
    settle = advanced("capture.idle_settle")

    chunks: list[str] = []
    deadline = time.time() + timeout
    last_data = time.time()

    while time.time() < deadline:
        try:
            data = channel.recv(65536)
            if not data:
                break
            chunks.append(data.decode("utf-8", errors="replace"))
            last_data = time.time()
        except socket.timeout:
            if time.time() - last_data > settle and chunks:
                break
        except Exception:
            break

    return "".join(chunks)


def capture_config(session: dict) -> dict:
    """
    Capture the running configuration over a secondary channel.

    Args:
        session: The session dict from SessionManager.

    Returns:
        The result of storing it, including whether it differed from the
        previous snapshot.

    Raises:
        ConnectionError_: With a message explaining why capture is not
            possible on this session.
    """
    handler = session.get("handler")
    if not handler or not getattr(handler, "is_connected", False):
        raise ConnectionError_("The session is no longer connected.")

    platform = session_platform(session)
    profile = get_profile(platform)
    commands = {
        "paging_off": profile.paging_off or "terminal length 0",
        "show_run":   profile.show_run or "show running-config",
    }

    # A second channel first, always. It cannot disturb what the user is
    # typing and it gets its own wide PTY, so paging never engages — both of
    # which the live channel has to work around rather than avoid.
    channel = (handler.open_secondary_channel()
               if isinstance(handler, SSHHandler) else None)

    if channel is not None:
        via = "second channel"
        try:
            # Clear the login banner before asking anything.
            _read_until_idle(channel, timeout=3.0)

            channel.send((commands["paging_off"] + "\n").encode())
            _read_until_idle(channel, timeout=5.0)

            channel.send((commands["show_run"] + "\n").encode())
            raw = _read_until_idle(channel)
        finally:
            try:
                channel.close()
            except Exception:
                pass
    else:
        # Serial and telnet cannot multiplex at all, and a switch that limits
        # concurrent sessions refuses one — which used to mean capture, drift
        # detection and the whole diff feature never worked on those devices.
        via = "live session"
        raw = capture_config_live(session)

    config = _tidy_config(raw, commands["show_run"])
    if not config.strip():
        raise ConnectionError_(
            f"The device returned nothing for '{commands['show_run']}'. It may "
            f"use a different command, or the account may lack privilege."
        )

    hostname = session.get("hostname") or ""
    result = store.add_snapshot(hostname, config, session.get("session_id", ""))
    result["platform"] = platform
    result["line_count"] = config.count("\n") + 1
    # How it was captured, so the interface can say when it went through the
    # user's own session. Nothing here is sent silently — the paging command
    # is echoed and announced for the same reason — and a capture deliberately
    # hidden while it ran has to be stated once it has finished.
    result["via"] = via

    # A copy as a file, where the user asked for it. Redacted, capped, and
    # never allowed to turn a successful capture into a failed one — the
    # snapshot above is already stored and the drift check depends on it.
    try:
        result["archive"] = config_archive.archive(
            hostname, config, changed=bool(result.get("stored")))
    except Exception as exc:                                # pragma: no cover
        logger.warning("Could not archive the capture from %s: %s", hostname, exc)
        result["archive"] = {"written": False, "reason": str(exc)}

    logger.info(
        "Captured %s lines of config from %s over the %s (%s)%s",
        result["line_count"], hostname, via,
        "changed" if result["stored"] else "unchanged",
        f"; saved to {result['archive']['path']}" if result["archive"].get("written") else "",
    )
    return result


def _tidy_config(raw: str, command: str) -> str:
    """
    Reduce captured output to just the configuration.

    Strips the echoed command, pager artefacts and the trailing prompt, so two
    snapshots of an unchanged device hash identically instead of differing by
    a prompt.
    """
    text = strip_pager_prompts(clean(raw))
    lines = text.splitlines()

    # Drop everything up to and including the echo of the command itself.
    for index, line in enumerate(lines):
        if command in line:
            lines = lines[index + 1:]
            break

    # Drop a trailing prompt left by the device waiting for more input.
    from backend.session.transcript import match_prompt
    while lines and (not lines[-1].strip() or match_prompt(lines[-1])):
        lines.pop()

    return "\n".join(lines).strip("\n")


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def diff_snapshots(old: dict, new: dict) -> dict:
    """
    Return a unified diff between two snapshots.

    Args:
        old: The earlier snapshot row.
        new: The later snapshot row.

    Returns:
        The diff text plus added/removed counts.
    """
    old_lines = (old.get("content") or "").splitlines()
    new_lines = (new.get("content") or "").splitlines()

    from backend.advanced import get as advanced

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        n=int(advanced("capture.diff_context")),
        fromfile=f"{old.get('hostname', '')} @ {_when(old.get('captured_at'))}",
        tofile=f"{new.get('hostname', '')} @ {_when(new.get('captured_at'))}",
        lineterm="",
    ))

    # Count only real changes: the +++/--- header lines start with the same
    # characters and would otherwise inflate the totals by one each.
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    return {
        "diff":    "\n".join(diff),
        "added":   added,
        "removed": removed,
        "changed": added + removed,
        "old_id":  old.get("id"),
        "new_id":  new.get("id"),
    }


def drift_report(session: dict) -> dict:
    """
    Compare the device's configuration now against the last visit.

    Returns a structure the UI can show as a one-line summary. Never raises:
    a device that will not give up its configuration should produce an
    explanation, not an error banner on an otherwise fine session.
    """
    hostname = session.get("hostname") or ""

    if not config_archive.capture_enabled():
        return {
            "available": False, "hostname": hostname,
            "capture": {"captured": False,
                        "reason": "capture is switched off"},
            "reason": "Configuration capture is switched off under "
                      "Settings → Session Logging.",
        }

    previous = store.latest_snapshot(hostname)

    try:
        result = capture_config(session)
    except ConnectionError_ as exc:
        return {"available": False, "reason": str(exc), "hostname": hostname,
                "capture": {"captured": False, "reason": str(exc)}}

    # What was saved as a file, if anything — the confirmation the user gets
    # for a capture that is otherwise entirely invisible to them.
    archive = result.get("archive") or {}

    # What happened to the snapshot itself, as distinct from what happened to
    # the optional file. The only confirmation anywhere was tied to the file,
    # and file archiving is off by default — so a capture that worked
    # perfectly said nothing at all.
    capture = {
        "captured":   True,
        "lines":      result.get("line_count", 0),
        "stored":     bool(result.get("stored")),
        "unchanged":  bool(result.get("unchanged")),
        # "second channel" or "live session". A capture through the user's own
        # session is hidden while it runs, so this is the whole of how they
        # find out it happened — nothing in ShellMate is sent silently.
        "via":        result.get("via", "second channel"),
    }

    if previous is None:
        return {
            "available": True, "first_visit": True, "hostname": hostname,
            "archive": archive, "capture": capture,
            "summary": f"First configuration snapshot of {hostname} stored "
                       f"({result['line_count']:,} lines).",
        }

    current = store.latest_snapshot(hostname)

    if result.get("unchanged"):
        report = {
            "available": True, "first_visit": False, "changed": 0,
            "hostname": hostname,
            "archive": archive, "capture": capture,
            "days_since": _days_since(previous.get("captured_at")),
            "summary": _unchanged_summary(hostname, previous.get("captured_at")),
        }
        return _with_baseline(report, hostname, current or previous)

    comparison = diff_snapshots(previous, current or {})

    report = {
        "available":   True,
        "first_visit": False,
        "hostname":    hostname,
        "archive":     archive,
        "capture":     capture,
        "days_since":  _days_since(previous.get("captured_at")),
        "changed":     comparison["changed"],
        "added":       comparison["added"],
        "removed":     comparison["removed"],
        "diff":        comparison["diff"],
        "summary":     _changed_summary(
            previous.get("captured_at"), comparison["changed"],
        ),
    }
    return _with_baseline(report, hostname, current or {})


def _with_baseline(report: dict, hostname: str, current: dict) -> dict:
    """
    Add the comparison against a pinned baseline, when there is one.

    "Since your last visit" is an accident of when somebody happened to log
    in, and looking at a device consumes it: connect, see four lines changed,
    reconnect an hour later to investigate, and you are told nothing has
    changed — the evidence is one row further back with no way to reach it.

    A baseline is a moment somebody chose, so it survives being looked at.
    Reported *alongside* the last-visit number rather than instead of it: they
    answer different questions and the honest thing is to give both.
    """
    from backend.advanced import get as advanced

    mode = advanced("capture.baseline_mode")
    if mode == "last-visit":
        return report

    baseline = store.get_baseline(hostname)
    if not baseline or not current:
        return report

    if int(baseline.get("id", 0)) == int(current.get("id", 0)):
        report["baseline"] = {
            "set_at": baseline.get("baseline_set_at"),
            "note":   baseline.get("baseline_note", ""),
            "changed": 0,
            "summary": "Identical to the pinned baseline.",
        }
        return report

    against = diff_snapshots(baseline, current)
    report["baseline"] = {
        "set_at":   baseline.get("baseline_set_at"),
        "note":     baseline.get("baseline_note", ""),
        "changed":  against["changed"],
        "added":    against["added"],
        "removed":  against["removed"],
        "diff":     against["diff"],
        "summary":  (f"{against['changed']} line"
                     f"{'' if against['changed'] == 1 else 's'} differ from the "
                     f"baseline set {_when(baseline.get('baseline_set_at'))}."),
    }
    return report


def _days_since(timestamp: float | None) -> int | None:
    if not timestamp:
        return None
    return max(0, int((time.time() - timestamp) / 86400))


def _when(timestamp: float | None) -> str:
    if not timestamp:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _ago(timestamp: float | None) -> str:
    """Phrase an interval the way someone would say it out loud."""
    days = _days_since(timestamp)
    if days is None:
        return "previously"
    if days == 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _unchanged_summary(hostname: str, timestamp: float | None) -> str:
    return f"You were last on {hostname} {_ago(timestamp)}. Nothing has changed since."


def _changed_summary(timestamp: float | None, changed: int) -> str:
    lines = "line has" if changed == 1 else "lines have"
    return f"You were last here {_ago(timestamp)}, and {changed} {lines} changed since."


# ---------------------------------------------------------------------------
# Capturing over the live channel
# ---------------------------------------------------------------------------

class LiveCapture:
    """
    Capture a configuration through the session the user is already using.

    A second SSH channel is the right way to do this and stays the default:
    it cannot disturb what somebody is typing, and it gets its own wide PTY so
    paging never engages. But a great many switches limit concurrent sessions
    and refuse one — and on those devices capture, drift detection and the
    whole diff feature never worked at all. "Cannot use the live channel" is
    not the same as "cannot use the live channel *invisibly*".

    The state machine lives here; the terminal read loop drives it, because
    that loop already owns the channel. Two readers on one channel would race
    for every byte, so this one never reads — it is handed text.

    Three things have to hold for a capture not to be mistaken for something
    the user did, and each is a way this goes wrong:

    - It must not reach the browser, or they watch their screen fill with a
      configuration they did not ask for.
    - It must not reach the session buffer, the transcript or the log, or it
      becomes evidence of what happened in the session — sent to the AI as
      context and recorded in history as a command that was typed.
    - It must not start mid-command. `transcript.last_prompt` is the existing
      signal for "idle at a prompt", the same one onboarding waits for.

    And afterwards it must be said. ShellMate's rule is that nothing is sent
    silently, so this is hidden *while it happens* and stated *when it is
    done* — which is the honest shape, rather than genuinely invisible.
    """

    #: How a device says it is waiting to print more: `--More--`, `--more--`,
    #: `--More(50%)--`, `---(more)---`, `<--- More --->`, `---- More ----`.
    #:
    #: **Anchored at the end**, which is not cosmetic. A pager prompt is by
    #: definition the last thing on the stream, because the device has stopped
    #: to wait. Searching the accumulated text instead meant the marker stayed
    #: in the tail after being answered, so every subsequent chunk answered it
    #: again — a stream of stray spaces into a live session.
    #:
    #: Trailing whitespace and backspaces are stripped first: a device erases
    #: its own pager prompt with backspaces once it has an answer.
    PAGER_RE = re.compile(
        r"(?:<-+\s*more\s*-+>"
        r"|-{2,}\s*\(?\s*more\s*(?:\([^)]*\))?\s*\)?\s*-{2,})"
        r"[\s\x08]*$",
        re.IGNORECASE)

    def __init__(self, command: str, prompt: str, timeout: float,
                 settle: float) -> None:
        self.command = command
        #: The prompt the device was sitting at. The capture ends when it
        #: comes back, which is a far tighter signal than waiting for silence.
        self.prompt = prompt
        self.timeout = timeout
        self.settle = settle

        self.state = "pending"          # pending -> running -> done | aborted
        self.reason = ""
        self.text = ""
        self._chunks: list[str] = []
        self._started_at = 0.0
        self._last_data = 0.0
        #: Set when the capture reaches a terminal state, whichever one.
        #: capture_config() waits on this from its worker thread.
        self.done = threading.Event()

    # -- driven by the read loop --------------------------------------------

    @property
    def active(self) -> bool:
        return self.state == "running"

    def ready_to_start(self, at_prompt: bool) -> bool:
        """Whether the device is idle at a prompt and nothing has been sent."""
        return self.state == "pending" and at_prompt

    def started(self) -> None:
        """Record that the command has gone to the device."""
        self.state = "running"
        self._started_at = time.monotonic()
        self._last_data = self._started_at

    def feed(self, text: str) -> str | None:
        """
        Take a chunk of device output.

        Returns a string to send back to the device — a space to advance a
        pager — or None. The caller withholds the text either way; that
        decision is `active`, not this return value.
        """
        self._chunks.append(text)
        self._last_data = time.monotonic()

        joined = "".join(self._chunks)

        # A pager on the *live* channel is expected: the second channel gets a
        # 200x1000 PTY precisely so this never happens, and here the session
        # has whatever geometry the user's window has. Answering the pager
        # keeps the device's own settings untouched — sending `terminal
        # length 0` would change their session's state to suit us, and outlast
        # the capture.
        if self.PAGER_RE.search(joined[-200:]):
            return " "

        # Finished when the prompt it started from comes back at the end.
        if self._at_prompt(joined):
            self._finish(joined)
        elif time.monotonic() - self._started_at > self.timeout:
            self.abort("the device was still printing after the capture timeout")
        return None

    def tick(self) -> None:
        """
        Called on an idle pass of the read loop.

        A device that stops mid-configuration without printing its prompt
        again would otherwise hold the capture open until the timeout. The
        quiet period is the same one the second-channel path uses.
        """
        if not self.active:
            return
        now = time.monotonic()
        if now - self._started_at > self.timeout:
            self.abort("the device was still printing after the capture timeout")
        elif self._chunks and now - self._last_data > self.settle:
            self._finish("".join(self._chunks))

    def abort(self, reason: str) -> None:
        """Give up, releasing the session back to the user immediately."""
        if self.done.is_set():
            return
        self.state = "aborted"
        self.reason = reason
        self.text = ""
        self.done.set()

    # -- internals -----------------------------------------------------------

    def _at_prompt(self, joined: str) -> bool:
        """
        Whether the device has finished and is waiting again.

        The echoed command is skipped: `show running-config` is typed at a
        prompt, so the prompt appears in the first line of what comes back and
        matching on it would end the capture before any configuration arrived.
        """
        body = joined.split("\n", 1)[-1] if "\n" in joined else ""
        if not body.strip():
            return False

        last_line = body.rsplit("\n", 1)[-1].strip()
        if not last_line:
            return False
        if self.prompt and last_line.startswith(self.prompt.strip()):
            return True
        return match_prompt(last_line) is not None

    def _finish(self, joined: str) -> None:
        self.state = "done"
        self.text = joined
        self.done.set()


def _live_capture_allowed(session: dict) -> tuple[bool, str]:
    """Whether a live-channel capture may be attempted, and why not."""
    if not advanced("capture.live_fallback"):
        return False, ("This device refused a second channel, and capturing "
                       "over your live session is switched off under "
                       "Settings → Advanced → Configuration capture.")
    if not session.get("is_connected", True):
        return False, "The session is no longer connected."
    return True, ""


def capture_config_live(session: dict) -> str:
    """
    Run a capture through the session's own channel and return the raw output.

    Blocks the calling worker thread until the read loop reports the capture
    finished, aborted, or ran out of time. Raises ConnectionError_ with an
    explanation in every case that produced nothing.
    """
    allowed, why = _live_capture_allowed(session)
    if not allowed:
        raise ConnectionError_(why)

    if session.get("live_capture") is not None:
        raise ConnectionError_("A capture is already running on this session.")

    platform = session_platform(session)
    profile = get_profile(platform)
    command = profile.show_run or "show running-config"

    timeout = float(advanced("capture.timeout"))
    capture = LiveCapture(
        command=command,
        prompt=session["transcript"].last_prompt,
        timeout=timeout,
        settle=float(advanced("capture.idle_settle")),
    )
    session["live_capture"] = capture

    try:
        # A little longer than the capture's own timeout, so the state machine
        # gets to set its own reason rather than this racing it to a vaguer
        # one. The wait is bounded either way: a read loop that has died must
        # not strand this thread for the life of the process.
        if not capture.done.wait(timeout + 15.0):
            capture.abort("the session stopped responding")

        if capture.state != "done":
            raise ConnectionError_(
                f"Capture over your live session did not complete: "
                f"{capture.reason}."
            )
        return capture.text
    finally:
        session["live_capture"] = None
