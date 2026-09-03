"""
test_claude_client.py — What the Claude client sends, and how it reads a refusal.

Two things went wrong at once and produced one misleading message: the
Claude 5 family rejects `temperature` with a 400, and the error mapper
treated any 400 whose text mentioned "model" as a retired model. Every
model in the picker then "did not exist". Offline checks, no network:

- which models are sent a temperature, and that the answer is learned when
  a model refuses one
- that only a genuine not-found is reported as a retired model
- that the request retries exactly once without the parameter and streams
  the second answer, using a fake transport

    python test_claude_client.py
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-claude-"))
paths._data_dir_cache = _TEMP

import httpx                                                          # noqa: E402

from backend.ai import claude_client                                  # noqa: E402

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


def test_sampling_gate() -> None:
    print("\n-- Which models get a temperature --")
    claude_client._NO_SAMPLING.clear()
    for model, expected in (
        ("claude-sonnet-5", False), ("claude-fable-5-1", False), ("claude-opus-5", False),
        ("claude-opus-4-7", False), ("claude-opus-4-8", False),
        ("claude-sonnet-4-6", True), ("claude-opus-4-6", True), ("claude-haiku-4-5", True),
    ):
        check(f"{model}: {'sent' if expected else 'not sent'}",
              claude_client._accepts_sampling(model) is expected)
    claude_client._NO_SAMPLING.add("claude-sonnet-4-6")
    check("a model that refused it once is not sent it again",
          claude_client._accepts_sampling("claude-sonnet-4-6") is False)
    claude_client._NO_SAMPLING.clear()


def test_error_mapping() -> None:
    print("\n-- Reading a refusal --")
    temperature = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error", "message": "`temperature` is deprecated for this model."}}).encode()
    text = claude_client._explain_api_error(400, temperature, "claude-sonnet-5")
    check("a 400 about temperature is not called a retired model",
          "retired" not in text and "temperature" in text, text)
    missing = json.dumps({"type": "error", "error": {
        "type": "not_found_error", "message": "model: claude-x"}}).encode()
    check("a not_found_error is", "retired" in claude_client._explain_api_error(404, missing, "claude-x"))
    check("  even as a 400 with the not-found type",
          "retired" in claude_client._explain_api_error(400, missing, "claude-x"))
    check("401 is the key", "API key" in claude_client._explain_api_error(401, b"{}", "m"))


def test_retry_without_temperature() -> None:
    print("\n-- One retry, then the answer --")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "`temperature` is deprecated for this model."}})
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 0}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ready"}},
            {"type": "message_delta", "usage": {"output_tokens": 1}},
        ]
        stream = "".join(f"data: {json.dumps(e)}\n" for e in events)
        return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class Patched(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    claude_client.httpx.AsyncClient = Patched
    claude_client._NO_SAMPLING.clear()
    from backend import settings_store
    original = settings_store.get_effective
    claude_client.get_effective = lambda key, fallback: "sk-test" if key == "anthropic_api_key" else original(key, fallback)
    try:
        async def go():
            out = []
            # A model this code does not know about, so the temperature is sent.
            async for chunk in claude_client.stream_response("hi", "", model="claude-future-9"):
                out.append(chunk)
            return out
        out = asyncio.run(go())
    finally:
        claude_client.httpx.AsyncClient = real_client
        claude_client.get_effective = original

    check("the first request carried the temperature", seen and "temperature" in seen[0], str(seen[:1]))
    check("the second did not", len(seen) == 2 and "temperature" not in seen[1], str(len(seen)))
    check("the answer streamed through", "ready" in "".join(c for c in out if isinstance(c, str)), str(out))
    check("usage followed it", any(isinstance(c, dict) and c["usage"]["input"] == 10 for c in out))
    check("the model is remembered as refusing sampling", "claude-future-9" in claude_client._NO_SAMPLING)


def test_error_event_inside_the_stream() -> None:
    """An overloaded_error can arrive as an event on the 200 (#500)."""
    print("\n-- An error event on a 200 --")

    def handler(request: httpx.Request) -> httpx.Response:
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ]
        stream = "".join(f"data: {json.dumps(e)}\n" for e in events)
        return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})

    real_client = httpx.AsyncClient

    class Patched(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    claude_client.httpx.AsyncClient = Patched
    from backend import settings_store
    original = settings_store.get_effective
    claude_client.get_effective = lambda key, fallback: "sk-test"
    try:
        async def go():
            async for _ in claude_client.stream_response("hi", "", model="claude-sonnet-5"):
                pass
        try:
            asyncio.run(go())
            check("the error event raises", False, "the stream ended quietly")
        except ValueError as exc:
            check("the error event raises with Claude's message", "Overloaded" in str(exc), str(exc))
    finally:
        claude_client.httpx.AsyncClient = real_client
        claude_client.get_effective = original


def main() -> int:
    print("=" * 52)
    print("  Claude client")
    print("=" * 52)
    for test in (test_sampling_gate, test_error_mapping, test_retry_without_temperature,
                 test_error_event_inside_the_stream):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
    shutil.rmtree(_TEMP, ignore_errors=True)
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
