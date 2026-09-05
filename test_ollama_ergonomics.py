"""
test_ollama_ergonomics.py — Local-model ergonomics: speed, truncation, pulls (#555).

Three things a local model needs that a hosted one does not, and each has a
failure that is invisible on screen:

- **Tokens per second.** Ollama sends `eval_duration` and the client used to
  discard it. A local run has no bill and no dashboard, so this is the only
  signal that the model is too big for the machine — and a divide by a
  duration that is absent or zero (a cached prompt, a cancelled generation,
  an older build) must report nothing rather than crash the reply.
- **The truncation warning.** Ollama does not refuse a prompt longer than
  `num_ctx`; it cuts it from the front and answers confidently about output
  it never read. The warning must be raised when the window is known and
  reached — and must *not* be invented when the setting is 0, where the
  limit is whatever the model was built with and the response never says.
- **The pull.** Gigabytes on a background thread: it has to be reportable,
  refusable while one is running, cancellable, and it must fail in a
  sentence rather than a traceback, because the commonest reason it fails is
  an air-gapped machine, which is not a bug.

Nothing here touches a real Ollama; the streams are faked.

    python test_ollama_ergonomics.py
"""

import asyncio
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ollama-"))
paths._data_dir_cache = _TEMP

import httpx                                                        # noqa: E402

from backend import ollama_pull                                     # noqa: E402
from backend.advanced import get as real_advanced                   # noqa: E402
from backend.ai import ollama_client                                # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ------------------------------------------------------------ the client
def drive(done_event: dict, num_ctx: int = 8192) -> tuple[list, dict]:
    """
    Run `stream_response` against a fake Ollama. Returns (chunks, payload).

    The whole client is exercised rather than the usage helper alone,
    because the window reported has to be the one *this request asked for* —
    a figure read from anywhere else could disagree with what Ollama was
    actually told.
    """
    body = (json.dumps({"message": {"content": "Gi0/2 is err-disabled."}}) + "\n"
            + json.dumps(dict(done_event, done=True)) + "\n").encode()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    original_shared = ollama_client.http.shared
    original_advanced = ollama_client.advanced
    ollama_client.http.shared = lambda name, timeout: client
    ollama_client.advanced = (
        lambda key: num_ctx if key == "ai.ollama_num_ctx" else real_advanced(key))
    try:
        async def go():
            out = []
            async for chunk in ollama_client.stream_response("why?", "", model="qwen2.5:7b"):
                out.append(chunk)
            await client.aclose()
            return out
        chunks = asyncio.run(go())
    finally:
        ollama_client.http.shared = original_shared
        ollama_client.advanced = original_advanced
    return chunks, seen


def usage_of(chunks: list) -> dict:
    for chunk in chunks:
        if isinstance(chunk, dict) and "usage" in chunk:
            return chunk["usage"]
    return {}


def test_tokens_per_second() -> None:
    print("\n-- Tokens per second --")
    chunks, _ = drive({"prompt_eval_count": 1200, "eval_count": 250,
                       "eval_duration": 5_000_000_000})
    usage = usage_of(chunks)
    check("the reply still streams before the usage",
          chunks and chunks[0] == "Gi0/2 is err-disabled.", str(chunks[:1]))
    check("250 tokens in 5 seconds is 50.0 per second",
          usage.get("tokens_per_second") == 50.0, str(usage))

    chunks, _ = drive({"prompt_eval_count": 10, "eval_count": 7,
                       "eval_duration": 3_000_000_000})
    check("it is rounded to one decimal place",
          usage_of(chunks).get("tokens_per_second") == 2.3,
          str(usage_of(chunks)))

    chunks, _ = drive({"prompt_eval_count": 10, "eval_count": 7,
                       "eval_duration": 0})
    check("a zero duration reports no rate rather than dividing by it",
          "tokens_per_second" not in usage_of(chunks), str(usage_of(chunks)))

    chunks, _ = drive({"prompt_eval_count": 10, "eval_count": 7})
    check("a missing duration reports no rate",
          "tokens_per_second" not in usage_of(chunks), str(usage_of(chunks)))


def test_the_existing_usage_shape_is_untouched() -> None:
    print("\n-- The four keys every provider shares --")
    usage = usage_of(drive({"prompt_eval_count": 1200, "eval_count": 250,
                            "eval_duration": 5_000_000_000})[0])
    check("provider, input, output and cache_read are exactly as they were",
          usage.get("provider") == "ollama" and usage.get("input") == 1200
          and usage.get("output") == 250 and usage.get("cache_read") == 0,
          str(usage))
    usage = usage_of(drive({"eval_count": 3})[0])
    check("a done event with only one of the counts still reports usage",
          usage.get("provider") == "ollama" and usage.get("input") == 0
          and usage.get("output") == 3, str(usage))


def test_the_truncation_warning() -> None:
    print("\n-- The context window, when it is known --")
    chunks, payload = drive({"prompt_eval_count": 4096, "eval_count": 20,
                             "eval_duration": 1_000_000_000}, num_ctx=4096)
    usage = usage_of(chunks)
    check("the request did ask for that window",
          payload.get("options", {}).get("num_ctx") == 4096, str(payload.get("options")))
    check("a prompt that reaches the window raises the warning",
          usage.get("context_full") is True and usage.get("context_limit") == 4096,
          str(usage))

    chunks, _ = drive({"prompt_eval_count": 9000, "eval_count": 20,
                       "eval_duration": 1_000_000_000}, num_ctx=4096)
    check("  and one that overruns it does too",
          usage_of(chunks).get("context_full") is True, str(usage_of(chunks)))

    chunks, _ = drive({"prompt_eval_count": 4095, "eval_count": 20,
                       "eval_duration": 1_000_000_000}, num_ctx=4096)
    usage = usage_of(chunks)
    check("a prompt inside the window says nothing at all",
          "context_full" not in usage and "context_limit" not in usage, str(usage))

    chunks, payload = drive({"prompt_eval_count": 999_999, "eval_count": 20,
                             "eval_duration": 1_000_000_000}, num_ctx=0)
    usage = usage_of(chunks)
    check("with the setting at 0 no num_ctx is sent",
          "num_ctx" not in payload.get("options", {}), str(payload.get("options")))
    check("  so no limit is invented, however long the prompt was",
          "context_full" not in usage and "context_limit" not in usage, str(usage))
    check("  but the speed is still reported",
          usage.get("tokens_per_second") == 20.0, str(usage))


# -------------------------------------------------------------- the pull
class PatchedClient(httpx.Client):
    """`ollama_pull` imports httpx inside the thread, so this swap reaches it."""
    transport = None

    def __init__(self, *a, **kw):
        kw["transport"] = PatchedClient.transport
        super().__init__(*a, **kw)


def lines_transport(*lines: dict, gate: threading.Event | None = None,
                    status: int = 200, raises: Exception | None = None):
    """A fake /api/pull. *gate* holds the stream open after the first line."""
    def stream():
        for index, event in enumerate(lines):
            if gate is not None and index == 1:
                gate.wait(10.0)
            yield (json.dumps(event) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if raises is not None:
            raise raises
        if status != 200:
            return httpx.Response(status, text="no such model")
        return httpx.Response(200, content=stream())
    return httpx.MockTransport(handler)


def wait_for_phase(*phases, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = ollama_pull.state()
        if current["phase"] in phases:
            return current
        time.sleep(0.02)
    return ollama_pull.state()


def reset_pull() -> None:
    ollama_pull._cancel.clear()
    ollama_pull._set(phase="idle", model="", received=0, total=0,
                     status="", error="")


def test_the_pull_state_machine() -> None:
    print("\n-- idle, pulling, done --")
    real = httpx.Client
    httpx.Client = PatchedClient
    try:
        reset_pull()
        check("it starts idle", ollama_pull.state()["phase"] == "idle")
        PatchedClient.transport = lines_transport(
            {"status": "pulling manifest"},
            {"status": "pulling 8934d96d", "digest": "sha256:8934", "total": 1000, "completed": 400},
            {"status": "pulling 8934d96d", "digest": "sha256:8934", "total": 1000, "completed": 900},
            {"status": "verifying sha256 digest"},
            {"status": "success"})
        started = ollama_pull.start_pull("qwen2.5:7b")
        check("starting reports the phase and the model",
              started["phase"] == "pulling" and started["model"] == "qwen2.5:7b", str(started))
        final = wait_for_phase("done", "failed")
        check("a clean stream finishes as done", final["phase"] == "done", str(final))
        check("  and the figures are not sent back to zero by the last "
              "status-only lines",
              final["received"] == 900 and final["total"] == 1000, str(final))
        check("  with no error", final["error"] == "")

        check("a pull with no model named is refused",
              _raises(lambda: ollama_pull.start_pull("  "), ValueError))
    finally:
        httpx.Client = real
        reset_pull()


def test_one_pull_at_a_time() -> None:
    print("\n-- One at a time --")
    real = httpx.Client
    httpx.Client = PatchedClient
    gate = threading.Event()
    try:
        reset_pull()
        PatchedClient.transport = lines_transport(
            {"status": "pulling manifest"},
            {"status": "success"}, gate=gate)
        ollama_pull.start_pull("qwen2.5:7b")
        wait_for_status("pulling manifest")
        check("a second pull while one is running is refused",
              _raises(lambda: ollama_pull.start_pull("llama3.1:8b"), ValueError))
        check("  and the refusal names what is already running",
              "qwen2.5:7b" in _message(lambda: ollama_pull.start_pull("llama3.1:8b")))
        gate.set()
        final = wait_for_phase("done", "failed")
        check("  the first pull still finishes", final["phase"] == "done", str(final))
        # Accepted, not "in a particular phase at this instant". With
        # the gate released the thread can finish before start_pull
        # returns, so asserting "pulling" is a race that passes on an
        # idle machine and fails on a busy one.
        started = ollama_pull.start_pull("mistral:7b")
        check("  and a pull may be started once it has",
              started["phase"] in ("pulling", "done")
              and started["model"] == "mistral:7b", str(started))
        wait_for_phase("done", "failed", "idle")
    finally:
        gate.set()
        httpx.Client = real
        reset_pull()


def test_cancelling() -> None:
    print("\n-- Cancelling gigabytes --")
    real = httpx.Client
    httpx.Client = PatchedClient
    gate = threading.Event()
    try:
        reset_pull()
        check("there is nothing to cancel when nothing is running",
              ollama_pull.cancel_pull() is False)
        PatchedClient.transport = lines_transport(
            {"status": "pulling manifest"},
            {"status": "pulling 8934d96d", "total": 4_700_000_000, "completed": 12_000},
            {"status": "success"}, gate=gate)
        ollama_pull.start_pull("qwen2.5:14b")
        wait_for_status("pulling manifest")
        check("cancelling a running pull is accepted", ollama_pull.cancel_pull() is True)
        gate.set()                        # let the stream produce one more chunk
        final = wait_for_phase("idle", "failed", "done")
        check("a cancelled pull goes back to idle, not failed",
              final["phase"] == "idle", str(final))
        check("  and is not reported as an error", final["error"] == "", str(final))
    finally:
        gate.set()
        httpx.Client = real
        reset_pull()


def test_failures_read_like_sentences() -> None:
    print("\n-- Failures a person can act on --")
    real = httpx.Client
    httpx.Client = PatchedClient
    try:
        reset_pull()
        PatchedClient.transport = lines_transport(
            raises=httpx.ConnectError("connection refused"))
        ollama_pull.start_pull("qwen2.5:7b")
        final = wait_for_phase("failed", "done")
        check("Ollama not running is a sentence, not a traceback",
              final["phase"] == "failed" and "Could not reach Ollama" in final["error"]
              and "Traceback" not in final["error"], str(final))

        reset_pull()
        PatchedClient.transport = lines_transport(
            {"status": "pulling manifest"},
            {"error": "dial tcp: lookup registry.ollama.ai: no such host"})
        ollama_pull.start_pull("qwen2.5:7b")
        final = wait_for_phase("failed", "done")
        check("an air-gapped machine is told it cannot pull, and why",
              final["phase"] == "failed"
              and "no route to the internet" in final["error"], str(final))

        reset_pull()
        PatchedClient.transport = lines_transport({"error": "model 'nope' not found"})
        ollama_pull.start_pull("nope")
        final = wait_for_phase("failed", "done")
        check("a name Ollama does not know says so",
              final["phase"] == "failed" and "Check the name" in final["error"], str(final))

        reset_pull()
        PatchedClient.transport = lines_transport(status=404)
        ollama_pull.start_pull("qwen2.5:7b")
        final = wait_for_phase("failed", "done")
        check("a host that is not Ollama is a readable refusal",
              final["phase"] == "failed" and "older build" in final["error"], str(final))
    finally:
        httpx.Client = real
        reset_pull()


def test_the_recommended_list() -> None:
    print("\n-- The recommendations --")
    check("the list is short enough to read",
          0 < len(ollama_pull.RECOMMENDED) <= 6, str(len(ollama_pull.RECOMMENDED)))
    check("every entry has a name, a size and a reason",
          all(entry.get("name") and entry.get("size") and entry.get("why")
              for entry in ollama_pull.RECOMMENDED))
    check("  and the reason is one line, not a paragraph",
          all(len(entry["why"]) <= 200 and "\n" not in entry["why"]
              for entry in ollama_pull.RECOMMENDED))
    check("  the names carry an explicit tag, so nothing depends on :latest",
          all(":" in entry["name"] for entry in ollama_pull.RECOMMENDED))


# ------------------------------------------------------------- utilities
def wait_for_status(text: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ollama_pull.state()["status"] == text:
            return
        time.sleep(0.02)


def _raises(call, kind) -> bool:
    try:
        call()
    except kind:
        return True
    except Exception:
        return False
    return False


def _message(call) -> str:
    try:
        call()
    except Exception as exc:
        return str(exc)
    return ""


def test_the_integration_is_wired() -> None:
    """
    A module nothing loads, and a field nothing carries, are both features
    nobody has.
    """
    print("\n-- Wired up --")
    root = Path(__file__).parent
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    chat = (root / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
    pull = (root / "frontend" / "js" / "ollama_pull.js").read_text(encoding="utf-8")
    app = (root / "backend" / "app.py").read_text(encoding="utf-8")

    check("the routes exist",
          '"/api/ollama/pull"' in app and "ollama_pull_cancel" in app)
    check("a pull already running is a 409, not a 400",
          "status = 409" in app,
          "the request is well formed and the state is what is in the way")

    check("the panel is loaded", "js/ollama_pull.js" in html)
    check("and there is a control to pull with",
          'id="ollama-pull-start"' in html and 'id="setting-ollama-model"' in html)
    check("with a cancel, because a pull is gigabytes",
          'id="ollama-pull-cancel"' in html)
    check("the suggestions carry their reason, not just a name",
          "option.label" in pull,
          "qwen2.5:14b tells somebody choosing their first model nothing")
    check("polling stops when nothing is happening",
          "clearInterval(timer)" in pull,
          "a timer that runs forever is a request a second for the session")

    check("tokens per second survives into the meter",
          "tokens_per_second: Number(msg.tokens_per_second)" in chat,
          "lastUsage is rebuilt field by field, so anything not named there "
          "is silently dropped")
    check("and reaches the tooltip",
          "tokens/second on the last reply" in chat)

    check("the truncation warning exists",
          "_warnIfTruncated" in chat)
    check("it fires only when the model actually said so",
          "!msg.context_full" in chat,
          "warning on a guess would train people to ignore it")
    check("once per conversation",
          "truncationWarned" in chat,
          "a warning on every reply is one people stop reading, and the "
          "condition does not change until somebody acts on it")
    check("and it says what to do about it",
          "Raise the context window" in chat)


def main() -> int:


    print("=" * 60)
    print("  Local-model ergonomics — speed, truncation, pulling")
    print("=" * 60)

    for test in (
        test_tokens_per_second,
        test_the_existing_usage_shape_is_untouched,
        test_the_truncation_warning,
        test_the_pull_state_machine,
        test_one_pull_at_a_time,
        test_cancelling,
        test_failures_read_like_sentences,
        test_the_recommended_list,
    
        test_the_integration_is_wired,):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_TEMP, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 60)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
