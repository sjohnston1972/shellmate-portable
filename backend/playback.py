"""
playback.py — A recorded session as a page that replays itself (#574).

A replayed session could only be watched inside ShellMate. That is fine
while the person who needs to see it is the person who recorded it, and
useless the moment it is a vendor, a customer, or a colleague who does not
have ShellMate installed — which is most of the times anybody wants to
show somebody a session.

So: one HTML file, opened by double-clicking it, that plays the session
back with the same controls the panel has. The vendored xterm.js and the
session's commands go inside it. Nothing is fetched, because the file is
mailed, put on a share, or opened on an air-gapped machine, and a page that
fetches renders as a blank box exactly then.

**Two things here are load-bearing and neither announces itself.**

Device output goes into a `<script>` tag. A configuration containing the
characters that spell a closing script tag would end the block early and
put the rest of the session on the page as markup — so the payload's angle
brackets and ampersands are escaped as JSON unicode escapes, which no
amount of device output can undo. The two Unicode line separators go the
same way: they are whitespace to JSON and statement terminators to
JavaScript, so a device that emits one produces a page that parses
differently from the JSON it was built from.

And everything is redacted on the way in. This file is *built to be sent*,
which makes it the most exposed thing ShellMate writes.
"""

import html
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from backend.paths import reports_dir, resource_dir
from backend.report import _slug
from backend.session.outbound import redact_text

logger = logging.getLogger(__name__)

# The gap between two commands is capped before it is played, exactly as the
# in-app player caps it. Without this, a session where somebody went to lunch
# replays as twenty minutes of nothing, and the person watching concludes the
# file is broken rather than that they are watching a pause.
MAX_GAP_SECONDS = 60


def _vendor(name: str) -> str:
    """
    Read one vendored asset to inline it.

    From ``resource_dir()`` — under a one-file build the frontend is
    unpacked into a temporary directory, and anything resolved relative to
    ``__file__`` would be looked for somewhere the bootloader has already
    deleted.
    """
    path = resource_dir() / "frontend" / "vendor" / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not inline %s for a playback export: %s", name, exc)
        return ""


def _payload(commands: list[dict]) -> str:
    """
    The commands as JSON that cannot break out of a script tag.

    JSON's own escaping is not enough here. It is a data format with no
    opinion about the document it is embedded in, and the sequence that ends
    a script element is perfectly ordinary text as far as it is concerned.
    """
    rows = [
        {
            "prompt": redact_text(entry.get("prompt") or ""),
            "command": redact_text(entry.get("command") or ""),
            "output": redact_text(entry.get("output") or ""),
            "ran_at": float(entry.get("ran_at") or 0),
            "duration_ms": int(entry.get("duration_ms") or 0),
        }
        for entry in commands
    ]
    text = json.dumps(rows, ensure_ascii=False)
    # Angle brackets and ampersands can spell a closing tag or an entity;
    # U+2028 and U+2029 are whitespace to JSON and line terminators to
    # JavaScript, which is a parse error inside a string literal.
    #
    # Written as chr() rather than as themselves: those two are invisible in
    # an editor and indistinguishable from a space, so putting them here
    # literally would leave two characters nobody can see doing something
    # nobody can guess.
    for char, escape in ((chr(0x3c), chr(92) + "u003c"),
                         (chr(0x3e), chr(92) + "u003e"),
                         (chr(0x26), chr(92) + "u0026"),
                         (chr(0x2028), chr(92) + "u2028"),
                         (chr(0x2029), chr(92) + "u2029")):
        text = text.replace(char, escape)
    return text


_PLAYER_CSS = """
  :root { color-scheme: dark; }
  body { margin: 0; padding: 1.5rem; background: #14141c; color: #cdd6f4;
         font: 14px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  header { max-width: 1100px; margin: 0 auto 1rem; }
  h1 { margin: 0 0 0.35rem; font-size: 1.35rem; letter-spacing: -0.01em; }
  .meta { color: #9a9ab0; font-size: 13px; }
  .wrap { max-width: 1100px; margin: 0 auto; }
  .controls { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;
              margin: 1rem 0 0.6rem; }
  button, select { font: inherit; color: #cdd6f4; background: #23233150;
                   border: 1px solid #3a3a4c; border-radius: 6px;
                   padding: 0.4rem 0.85rem; cursor: pointer; }
  button:hover:not(:disabled), select:hover { border-color: #5b5bd6; }
  button:disabled { opacity: 0.45; cursor: default; }
  button:focus-visible, select:focus-visible, input:focus-visible {
    outline: 2px solid #89b4fa; outline-offset: 2px; }
  .progress { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.6rem; }
  input[type=range] { flex: 1; accent-color: #89b4fa; }
  .clock { font-variant-numeric: tabular-nums; color: #9a9ab0; font-size: 13px;
           white-space: nowrap; }
  .step { color: #9a9ab0; font-size: 13px; margin-bottom: 0.6rem;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #term { background: #1e1e2e; border: 1px solid #2c2c3c; border-radius: 8px;
          padding: 0.6rem; height: 62vh; min-height: 320px; }
  footer { max-width: 1100px; margin: 1.25rem auto 0; color: #71718a;
           font-size: 12px; border-top: 1px solid #2c2c3c; padding-top: 0.85rem; }
  .empty { color: #9a9ab0; padding: 2rem; text-align: center; }
"""

# Deliberately a separate player from the one in history.js rather than a
# shared file. That one reads the app's settings, its DOM ids and its theme
# tokens, none of which exist here — and a shared module would have to grow
# a parameter for each, in the application, to serve a file it never opens.
# The timing model is what has to agree, and that is the part copied.
_PLAYER_JS = """
(function () {
  var commands = window.__SHELLMATE_SESSION__ || [];
  var host = document.getElementById('term');
  var playBtn = document.getElementById('play');
  var pauseBtn = document.getElementById('pause');
  var stopBtn = document.getElementById('stop');
  var speedSel = document.getElementById('speed');
  var seek = document.getElementById('seek');
  var stepEl = document.getElementById('step');
  var clockEl = document.getElementById('clock');
  var player = null;

  if (!commands.length) {
    host.innerHTML = '<div class="empty">This session recorded no commands.</div>';
    playBtn.disabled = true;
    return;
  }
  seek.max = String(commands.length - 1);

  // The same capped-gap model the application uses, so a session does not
  // run to a different length depending on where it is watched.
  function span() {
    var marks = [], total = 0, previousEnd = null;
    for (var i = 0; i < commands.length; i++) {
      var ranAt = commands[i].ran_at || 0;
      var took = (commands[i].duration_ms || 0) / 1000;
      if (previousEnd !== null && ranAt) {
        total += Math.min(Math.max(0, ranAt - previousEnd), MAX_GAP);
      }
      marks.push(total);
      total += took;
      previousEnd = ranAt + took;
    }
    return { marks: marks, total: total };
  }

  function clock(seconds) {
    var s = Math.max(0, Math.round(seconds));
    var m = Math.floor(s / 60);
    return m + ':' + String(s % 60).padStart(2, '0');
  }

  function stop() {
    if (player) {
      player.cancelled = true;
      try { player.term.dispose(); } catch (e) { /* already gone */ }
      player = null;
    }
    stopBtn.disabled = true;
    pauseBtn.disabled = true;
    pauseBtn.textContent = 'Pause';
    playBtn.disabled = false;
  }

  function togglePause() {
    if (!player) return;
    player.paused = !player.paused;
    pauseBtn.textContent = player.paused ? 'Resume' : 'Pause';
  }

  function write(term, entry) {
    term.write((entry.prompt || '') + (entry.command || '') + '\\r\\n');
    var text = entry.output || '';
    term.write(text.charAt(text.length - 1) === '\\n' ? text : text + '\\r\\n');
  }

  async function play() {
    stop();
    host.innerHTML = '';
    var term = new window.Terminal({
      fontFamily: 'ui-monospace, Consolas, "Cascadia Mono", monospace',
      fontSize: 13, scrollback: 5000, disableStdin: true, convertEol: true,
      theme: { background: '#1e1e2e', foreground: '#cdd6f4' }
    });
    term.open(host);
    player = { term: term, cancelled: false, paused: false,
               speed: Number(speedSel.value) || 4, seekTo: null };
    playBtn.disabled = true;
    stopBtn.disabled = false;
    pauseBtn.disabled = false;

    var s = span();
    var marks = s.marks, total = s.total;

    function show(index, elapsed) {
      var entry = commands[index];
      if (document.activeElement !== seek) seek.value = String(index);
      stepEl.textContent = entry
        ? (index + 1) + ' of ' + commands.length + '  ' + (entry.command || '')
        : commands.length + ' of ' + commands.length;
      clockEl.textContent = clock(elapsed) + ' / ' + clock(total);
    }

    // One wait that honours pause, a speed change and a drag, all of which
    // can arrive while a slice is on screen.
    async function wait(ms) {
      var step = 60, left = ms;
      while (left > 0) {
        if (player.cancelled || player.seekTo !== null) return;
        if (player.paused) {
          await new Promise(function (r) { setTimeout(r, step); });
          continue;
        }
        var slice = Math.min(step, left / (player.speed || 1));
        await new Promise(function (r) { setTimeout(r, Math.max(0, slice)); });
        left -= slice * (player.speed || 1);
      }
    }

    var index = 0, previousEnd = null;
    while (index < commands.length) {
      if (player.cancelled) return;

      if (player.seekTo !== null) {
        var target = Math.min(Math.max(0, player.seekTo), commands.length - 1);
        player.seekTo = null;
        term.reset();
        for (var i = 0; i < target; i++) write(term, commands[i]);
        index = target;
        previousEnd = null;
        show(index, marks[index] || 0);
        continue;
      }

      var entry = commands[index];
      var ranAt = entry.ran_at || 0;
      if (previousEnd !== null && ranAt) {
        await wait(Math.min(Math.max(0, ranAt - previousEnd), MAX_GAP) * 1000);
        if (player.cancelled) return;
        if (player.seekTo !== null) continue;
      }

      show(index, marks[index] || 0);
      term.write((entry.prompt || '') + (entry.command || '') + '\\r\\n');
      await wait((entry.duration_ms || 0));
      if (player.cancelled) return;
      if (player.seekTo !== null) continue;

      var text = entry.output || '';
      term.write(text.charAt(text.length - 1) === '\\n' ? text : text + '\\r\\n');
      previousEnd = ranAt + (entry.duration_ms || 0) / 1000;
      index += 1;
    }

    show(commands.length, total);
    stopBtn.disabled = true;
    pauseBtn.disabled = true;
    playBtn.disabled = false;
  }

  playBtn.addEventListener('click', play);
  stopBtn.addEventListener('click', stop);
  pauseBtn.addEventListener('click', togglePause);
  seek.addEventListener('input', function () {
    if (player) player.seekTo = Number(seek.value);
  });
  speedSel.addEventListener('change', function () {
    if (player) player.speed = Number(speedSel.value) || 4;
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === ' ' && player) { e.preventDefault(); togglePause(); }
  });

  var s0 = span();
  clockEl.textContent = '0:00 / ' + clock(s0.total);
  stepEl.textContent = commands.length + ' commands recorded';
})();
"""


def _when(stamp) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(stamp)


def build(session: dict) -> tuple[str, str]:
    """
    Render the session as one self-contained page.

    Args:
        session: A store record from ``get_session``, with ``commands``.

    Returns:
        ``(title, html)``.
    """
    label = session.get("label") or session.get("hostname") or "session"
    title = f"Session playback — {label}"
    commands = session.get("commands") or []

    meta_parts = [
        (session.get("connection_type") or "").upper(),
        session.get("target") or session.get("hostname") or "",
        _when(session.get("started_at")),
        f"{len(commands)} commands",
    ]
    meta = " · ".join(part for part in meta_parts if part)

    notes = redact_text((session.get("notes") or "").strip())
    notes_html = (f"<p class=\"meta\">{html.escape(notes)}</p>" if notes else "")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    player_js = _PLAYER_JS.replace("MAX_GAP", str(MAX_GAP_SECONDS))

    return title, (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_vendor('xterm.css')}</style>\n"
        f"<style>{_PLAYER_CSS}</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        f"<h1>{html.escape(label)}</h1>\n"
        f'<p class="meta">{html.escape(meta)}</p>\n'
        f"{notes_html}\n"
        "</header>\n"
        '<div class="wrap">\n'
        '<div class="controls">\n'
        '<button type="button" id="play">Play</button>\n'
        '<button type="button" id="pause" disabled>Pause</button>\n'
        '<button type="button" id="stop" disabled>Stop</button>\n'
        '<label for="speed" class="clock">Speed</label>\n'
        '<select id="speed">\n'
        '<option value="1">1x</option>\n'
        '<option value="2">2x</option>\n'
        '<option value="4" selected>4x</option>\n'
        '<option value="8">8x</option>\n'
        '<option value="16">16x</option>\n'
        "</select>\n"
        "</div>\n"
        '<div class="progress">\n'
        '<input type="range" id="seek" min="0" max="0" value="0" step="1" '
        'aria-label="Jump to a command">\n'
        '<span class="clock" id="clock">0:00 / 0:00</span>\n'
        "</div>\n"
        '<div class="step" id="step"></div>\n'
        '<div id="term"></div>\n'
        "</div>\n"
        "<footer>Exported from ShellMate Portable on "
        f"{html.escape(stamp)}. Passwords and secrets are masked where "
        "ShellMate recognised them. Space pauses and resumes.</footer>\n"
        f"<script>{_vendor('xterm.js')}</script>\n"
        f"<script>window.__SHELLMATE_SESSION__ = {_payload(commands)};</script>\n"
        f"<script>{player_js}</script>\n"
        "</body>\n</html>\n"
    )


def transcript(session: dict) -> str:
    """
    The same session as plain text.

    The alternative to the playback, and not the same document as the
    Markdown report: this is what somebody pastes into a vendor case or a
    mail, where markup would arrive as literal asterisks and backticks.
    """
    label = session.get("label") or session.get("hostname") or "session"
    lines = [
        f"Session transcript — {label}",
        f"Host:      {session.get('hostname') or session.get('target') or ''}",
        f"Connection {(session.get('connection_type') or '').upper()}",
        f"Started:   {_when(session.get('started_at'))}",
        f"Ended:     {_when(session.get('ended_at')) or 'still open'}",
    ]
    notes = redact_text((session.get("notes") or "").strip())
    if notes:
        lines += ["", "Notes:", notes]
    lines += ["", "=" * 70, ""]

    commands = session.get("commands") or []
    if not commands:
        lines.append("No commands were recorded in this session.")
    for entry in commands:
        prompt = redact_text(entry.get("prompt") or "")
        command = redact_text((entry.get("command") or "").strip())
        lines.append(f"{prompt}{command}")
        output = redact_text(entry.get("output") or "").rstrip()
        if output:
            lines.append(output)
        lines.append("")

    lines += [
        "-" * 70,
        f"Exported from ShellMate Portable on "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
        "Passwords and secrets are masked where ShellMate recognised them.",
    ]
    return "\n".join(lines) + "\n"


def write(session: dict, fmt: str = "html") -> Path:
    """
    Write the playback or the transcript, returning the path.

    Args:
        session: A store record with ``commands``.
        fmt:     "html" for the self-contained replay, "txt" for the transcript.

    Raises:
        ValueError: ``fmt`` is neither "html" nor "txt".
        OSError:    The reports folder could not be written to.
    """
    if fmt not in ("html", "txt"):
        raise ValueError(f"Unknown playback format: {fmt!r}")

    device = session.get("label") or session.get("hostname") or "session"
    reports_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "playback.html" if fmt == "html" else "transcript.txt"
    path = reports_dir() / f"{_slug(device)}-{stamp}-{suffix}"

    text = build(session)[1] if fmt == "html" else transcript(session)
    path.write_text(text, encoding="utf-8")
    logger.info("Wrote a session %s to %s (%s bytes)", fmt, path, len(text))
    return path
