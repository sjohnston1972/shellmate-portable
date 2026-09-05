"""
ollama_pull.py — Fetch a local model without leaving the application (#555).

ShellMate's privacy story rests on Ollama: the buffer never leaves the
machine. Today a first-time user is sent away to a terminal to type
``ollama pull`` before the local provider can answer anything at all, and a
tool that cannot get itself into a working state has not really shipped the
feature. This drives Ollama's ``POST /api/pull``, which streams
newline-delimited JSON — ``status``, and for the blob layers ``digest``,
``total`` and ``completed`` — and turns it into one state dict the interface
polls.

The shape is deliberately `updater.py`'s, because the problem is the same
one: a long download on a background thread that a request has to be able to
ask about and to stop.

Three constraints, each learned from the thing it prevents:

- **A pull is gigabytes and must be cancellable.** Several minutes on a home
  connection, and the person who started it may simply have picked the wrong
  model. `_cancel` is checked on every chunk, so cancelling takes effect at
  the next line of progress rather than at the end of the download.
- **An air-gapped machine cannot pull at all.** That is not a bug and must
  not read like one. A refused connection to Ollama, and Ollama's own
  failure to reach the registry, both come back as a sentence saying the
  model has to be brought over by hand — not as a traceback in a dialog.
- **Nothing raises out of the thread.** A thread that dies with an exception
  leaves the phase on ``pulling`` for ever and the interface spinning at a
  download that stopped. Every failure lands in ``_state["error"]`` with the
  phase ``failed``.
"""

import json
import logging
import threading
import time

from backend.config import OLLAMA_HOST
from backend.settings_store import get_effective

logger = logging.getLogger(__name__)

# Long enough for a slow registry to think between progress lines, but not
# unbounded: a connection that has genuinely died must eventually say so
# rather than leave the phase on `pulling` until the application is closed.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 300.0

# A short list, because a long one is a research project rather than a
# recommendation. Every entry is a model that has been widely distributed
# through Ollama for a long time; sizes are the default (4-bit) tag and
# approximate, since a retag changes them. Each line says what it is *for
# this work* — reading device output and explaining it — because "good
# model" is not a reason to spend nine gigabytes.
RECOMMENDED: tuple[dict, ...] = (
    {"name": "qwen2.5:7b",  "size": "about 4.7 GB",
     "why": "The best first choice: it follows the command-suggestion format "
            "reliably and fits a laptop with 8 GB of memory."},
    {"name": "qwen2.5:14b", "size": "about 9 GB",
     "why": "ShellMate's default. Noticeably better at reasoning over a long "
            "configuration, if the machine has 16 GB to spare."},
    {"name": "llama3.1:8b", "size": "about 4.9 GB",
     "why": "Explains what it sees in plainer prose — a good pick when the "
            "session is being read by someone learning the platform."},
    {"name": "mistral:7b",  "size": "about 4.1 GB",
     "why": "The lightest of the four. Use it where memory is tight and the "
            "questions are short."},
)

_lock = threading.Lock()
_state: dict = {
    "phase": "idle",        # idle | pulling | done | failed
    "model": "",
    "received": 0,
    "total": 0,
    "status": "",           # Ollama's own words for the step in progress
    "error": "",
    "started": 0.0,
}
_cancel = threading.Event()


def state() -> dict:
    """What the pull is doing, as a copy — callers must not mutate it."""
    with _lock:
        return dict(_state)


def _set(**changes) -> None:
    with _lock:
        _state.update(changes)


def _host() -> str:
    return (get_effective("ollama_host", OLLAMA_HOST)
            or "http://localhost:11434").rstrip("/")


# ---------------------------------------------------------------- pulling
def start_pull(model: str) -> dict:
    """
    Begin pulling *model* on a daemon thread. Returns the state.

    Raises ValueError for the two things the caller can fix: no model named,
    and a pull already running. One at a time is not a simplification — two
    concurrent pulls would share this one progress dict and report a figure
    belonging to neither.
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("Name the model to pull, for example qwen2.5:7b.")
    with _lock:
        if _state["phase"] == "pulling":
            raise ValueError(
                f"{_state['model'] or 'A model'} is already being pulled. "
                "Wait for it to finish, or cancel it.")
    _cancel.clear()
    _set(phase="pulling", model=model, received=0, total=0,
         status="starting", error="", started=time.time())
    # Read before the thread is started, not after. A small model off a
    # local registry can be finished before the next line runs, and a
    # caller that asked to start a pull would then be told "done" for a
    # pull it never saw begin — which the panel renders as a progress bar
    # that never appears.
    started = state()
    threading.Thread(target=_pull, args=(model,), daemon=True,
                     name="ollama-pull").start()
    return started


def cancel_pull() -> bool:
    """
    Ask the running pull to stop. True if there was one to stop.

    The flag is set here and read by the thread on its next chunk; the state
    goes back to ``idle`` there rather than here, so the phase is never
    claimed to have changed before the download has actually let go.
    """
    with _lock:
        running = _state["phase"] == "pulling"
    if running:
        _cancel.set()
    return running


def _pull(model: str) -> None:
    """
    Stream ``/api/pull`` and report it. Never raises — see the module note.

    Ollama sends one JSON object per line: a bare ``status`` for the steps
    with nothing to measure ("pulling manifest", "verifying sha256 digest"),
    and ``digest``/``total``/``completed`` while a blob is coming down. The
    figures are per layer, not per model, so ``received`` and ``total``
    describe the layer in progress — which is what the progress bar wants,
    and what stops it running backwards when the next layer starts.
    """
    import httpx

    url = f"{_host()}/api/pull"
    # `model` is the current spelling and `name` the one older builds read.
    # Both are sent because an unknown field is ignored, while the wrong one
    # alone is a 400 that says nothing useful to the person reading it.
    body = {"model": model, "name": model, "stream": True}
    timeout = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    detail = resp.read().decode("utf-8", "replace").strip()
                    raise RuntimeError(_explain_status(model, resp.status_code, detail))
                for line in resp.iter_lines():
                    if _cancel.is_set():
                        raise InterruptedError("Cancelled.")
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue            # a partial line; the next one carries it
                    if not isinstance(event, dict):
                        continue
                    # Ollama reports a failure *after* the 200 as an error
                    # line in the stream. Read as progress it would look like
                    # a pull that simply stopped, which is the shape of bug
                    # that leaves somebody staring at a stalled bar.
                    if event.get("error"):
                        raise RuntimeError(_explain_error(str(event["error"])))
                    # The steps with nothing to measure — "verifying sha256
                    # digest", "writing manifest", "success" — carry a status
                    # and no figures. Taking those as zeros sent the bar back
                    # to the start just as the pull finished, so the figures
                    # are only touched by a line that actually has them.
                    progress = {"status": str(event.get("status") or "")}
                    if event.get("total") is not None:
                        progress["total"] = int(event.get("total") or 0)
                        progress["received"] = int(event.get("completed") or 0)
                    _set(**progress)
        _set(phase="done", status="success", error="")
        logger.info("Pulled Ollama model %s", model)
    except InterruptedError:
        _set(phase="idle", status="", received=0, total=0, error="")
        logger.info("The pull of %s was cancelled.", model)
    except Exception as exc:
        message = _explain_exception(exc)
        logger.warning("Pulling %s failed: %s", model, message)
        _set(phase="failed", error=message)


# ---------------------------------------------------------------- wording
def _explain_exception(exc: Exception) -> str:
    """
    A failure in the words of someone who has to decide what to do next.

    The two that matter are distinct and are constantly confused: Ollama is
    not running *here*, and Ollama is running but has no route *out*. The
    second is the ordinary state of a machine on an isolated management
    network, and the only answer is to carry the model over, so it says so.
    """
    import httpx

    if isinstance(exc, httpx.ConnectError):
        return (f"Could not reach Ollama at {_host()}. Is it running, and is "
                "the host in Settings correct?")
    if isinstance(exc, httpx.TimeoutException):
        return ("Ollama stopped sending progress. The pull may still be "
                "running on the Ollama host — check there before starting "
                "another.")
    text = str(exc).strip()
    return text or f"The pull failed: {type(exc).__name__}."


def _explain_error(detail: str) -> str:
    """Ollama's own error line, with the air-gapped case named."""
    lowered = detail.lower()
    offline = ("no such host", "dial tcp", "connection refused", "timeout",
               "network is unreachable", "lookup ", "tls", "proxy")
    if any(word in lowered for word in offline):
        return (f"Ollama could not reach the model registry: {detail[:300]}. "
                "A machine with no route to the internet cannot pull a model "
                "— copy one over with `ollama pull` on a connected machine "
                "and `ollama create`, or point Settings at an Ollama host "
                "that does have a route.")
    if "not found" in lowered or "file does not exist" in lowered:
        return (f"Ollama does not know that model: {detail[:300]}. Check the "
                "name and tag, for example qwen2.5:7b.")
    return f"Ollama refused the pull: {detail[:300]}"


def _explain_status(model: str, status: int, detail: str) -> str:
    """An HTTP failure before the stream ever started."""
    if status == 404:
        return (f"Ollama has no pull endpoint at {_host()} — it may be an "
                "older build, or the host may be something other than Ollama.")
    return f"Ollama answered {status} to the pull of {model}: {detail[:300]}"
