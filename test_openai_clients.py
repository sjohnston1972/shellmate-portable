"""
test_openai_clients.py — What the OpenAI-shaped clients send, and what they
make of a refusal.

OpenAI's reasoning models return 400 for `max_tokens` and for any
temperature but the default, and the picker offers them on purpose — so
every request to one failed with raw JSON in the chat bubble (#497). The
Claude client had the family gate and the learn-on-400 retry; OpenAI, xAI
and DeepSeek had neither. Offline, with a fake transport:

- which models are sent a temperature and which length parameter
- that a refusal is retried without the named parameter, once per
  parameter, and remembered for the run
- that the answer and the usage stream through the retry

    python test_openai_clients.py
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-openai-"))
paths._data_dir_cache = _TEMP

import httpx                                                          # noqa: E402

from backend.ai import openai_compat, openai_client, xai_client, deepseek_client  # noqa: E402

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


def forget() -> None:
    openai_compat._NO_SAMPLING.clear()
    openai_compat._COMPLETION_TOKENS.clear()


def test_parameter_gate() -> None:
    print("\n-- Which models get which parameters --")
    forget()
    P = openai_client.PROVIDER
    for model, sampling, length in (
        ("gpt-4o",       True,  "max_tokens"),
        ("gpt-4.1-mini", True,  "max_tokens"),
        ("o1",           False, "max_completion_tokens"),
        ("o3-mini",      False, "max_completion_tokens"),
        ("o4-mini",      False, "max_completion_tokens"),
        ("gpt-5",        False, "max_completion_tokens"),
        ("gpt-5-mini",   False, "max_completion_tokens"),
    ):
        check(f"openai {model}: temperature {'sent' if sampling else 'not sent'}, {length}",
              openai_compat.accepts_sampling(P, model) is sampling
              and openai_compat.length_parameter(P, model) == length)
    check("xAI assumes nothing about grok-4",
          openai_compat.accepts_sampling(xai_client.PROVIDER, "grok-4")
          and openai_compat.length_parameter(xai_client.PROVIDER, "grok-4") == "max_tokens")
    check("deepseek-reasoner is not sent a temperature",
          not openai_compat.accepts_sampling(deepseek_client.PROVIDER, "deepseek-reasoner"))
    check("  but deepseek-chat is",
          openai_compat.accepts_sampling(deepseek_client.PROVIDER, "deepseek-chat"))

    payload = openai_compat.build_payload(P, "o3-mini", [])
    check("a reasoning model's payload has neither max_tokens nor temperature",
          "max_tokens" not in payload and "temperature" not in payload
          and "max_completion_tokens" in payload, str(payload))
    payload = openai_compat.build_payload(P, "gpt-4o", [])
    check("an ordinary model's payload has both",
          "max_tokens" in payload and "temperature" in payload, str(payload))

    openai_compat._NO_SAMPLING.add("xai:grok-4")
    check("a model that refused a temperature once is not sent it again",
          not openai_compat.accepts_sampling(xai_client.PROVIDER, "grok-4"))
    check("  and the lesson is per provider",
          openai_compat.accepts_sampling(openai_client.PROVIDER, "grok-4"))
    forget()


def _drive(module, model: str, handler) -> tuple[list, list]:
    """Run one client against a fake transport; return (bodies seen, chunks)."""
    seen: list[dict] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(seen[-1])

    transport = httpx.MockTransport(recording)
    real_client = httpx.AsyncClient

    class Patched(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    openai_compat.httpx.AsyncClient = Patched
    original = module.get_effective
    module.get_effective = lambda key, fallback: "sk-test"
    try:
        async def go():
            out = []
            async for chunk in module.stream_response("hi", "", model=model):
                out.append(chunk)
            return out
        out = asyncio.run(go())
    finally:
        openai_compat.httpx.AsyncClient = real_client
        module.get_effective = original
    return seen, out


def _answer(text: str) -> httpx.Response:
    events = [
        {"choices": [{"delta": {"content": text}}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 1,
                                  "prompt_tokens_details": {"cached_tokens": 10}}},
    ]
    stream = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
    return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})


def _refuse(parameter: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {
        "message": f"Unsupported parameter: {parameter} is not supported with this model.",
        "type": "invalid_request_error", "param": parameter, "code": "unsupported_parameter"}})


def test_learns_both_parameters() -> None:
    print("\n-- A model this code does not know refuses both, one at a time --")
    forget()

    def handler(body: dict) -> httpx.Response:
        if "max_tokens" in body:
            return _refuse("max_tokens")
        if "temperature" in body:
            return _refuse("temperature")
        return _answer("ready")

    seen, out = _drive(openai_client, "gpt-9-reasoning", handler)
    check("three requests: the original and one retry per refused parameter",
          len(seen) == 3, str(len(seen)))
    check("the first carried max_tokens and temperature",
          seen and "max_tokens" in seen[0] and "temperature" in seen[0], str(seen[:1]))
    check("the second swapped in max_completion_tokens",
          len(seen) > 1 and "max_completion_tokens" in seen[1] and "max_tokens" not in seen[1], str(seen[1:2]))
    check("the third dropped the temperature",
          len(seen) > 2 and "temperature" not in seen[2] and "max_completion_tokens" in seen[2], str(seen[2:3]))
    check("the answer streamed through", "ready" in "".join(c for c in out if isinstance(c, str)), str(out))
    check("usage followed it", any(isinstance(c, dict) and "usage" in c for c in out), str(out))
    check("both lessons are remembered",
          "openai:gpt-9-reasoning" in openai_compat._NO_SAMPLING
          and "openai:gpt-9-reasoning" in openai_compat._COMPLETION_TOKENS)

    seen, out = _drive(openai_client, "gpt-9-reasoning", handler)
    check("the next request to that model gets it right first time",
          len(seen) == 1 and "temperature" not in seen[0] and "max_completion_tokens" in seen[0], str(seen))
    forget()


def test_known_family_first_time() -> None:
    print("\n-- A known reasoning model is right first time --")
    forget()
    seen, out = _drive(openai_client, "o3-mini", lambda body: _answer("ok"))
    check("one request, no temperature, max_completion_tokens",
          len(seen) == 1 and "temperature" not in seen[0]
          and "max_completion_tokens" in seen[0] and "max_tokens" not in seen[0], str(seen))


def test_other_providers_retry_too() -> None:
    print("\n-- xAI and DeepSeek learn the same way --")
    for module, model in ((xai_client, "grok-4"), (deepseek_client, "deepseek-future")):
        forget()

        def handler(body: dict) -> httpx.Response:
            return _refuse("temperature") if "temperature" in body else _answer("ok")

        seen, out = _drive(module, model, handler)
        check(f"{module.PROVIDER.label}: retried once without the temperature",
              len(seen) == 2 and "temperature" in seen[0] and "temperature" not in seen[1], str(seen))
        check("  and the usage names the provider",
              any(isinstance(c, dict) and c["usage"]["provider"] == module.PROVIDER.name for c in out), str(out))
    forget()


def test_usage_is_normalised() -> None:
    """`input` means the uncached prompt on every provider (#499)."""
    print("\n-- Usage --")
    forget()
    _, out = _drive(openai_client, "gpt-4o", lambda body: _answer("ok"))
    usage = next(c["usage"] for c in out if isinstance(c, dict))
    check("OpenAI: the cached portion is taken out of the input",
          usage["input"] == 20 and usage["cache_read"] == 10 and usage["output"] == 1, str(usage))
    deepseek = openai_compat.normalise_usage(
        {"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 64})
    check("DeepSeek: its top-level cache count is read the same way",
          deepseek == {"input": 36, "output": 5, "cache_read": 64}, str(deepseek))
    bare = openai_compat.normalise_usage({"prompt_tokens": 7, "completion_tokens": 2})
    check("no cache figures at all is not an error",
          bare == {"input": 7, "output": 2, "cache_read": 0}, str(bare))
    forget()


def test_an_error_inside_the_stream_surfaces() -> None:
    """A provider that fails after the 200 must not look like an empty answer (#500)."""
    print("\n-- An error event on a 200 --")
    forget()

    def mid_stream(body: dict) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": "part"}}]},
            {"error": {"message": "Rate limit reached for gpt-4o", "type": "rate_limit_error"}},
        ]
        stream = "".join(f"data: {json.dumps(e)}\n" for e in events)
        return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})

    try:
        _drive(openai_client, "gpt-4o", mid_stream)
        check("an error event raises", False, "the stream ended quietly")
    except ValueError as exc:
        check("an error event raises with the provider's message",
              "Rate limit reached" in str(exc), str(exc))
    forget()


def test_a_real_error_still_surfaces() -> None:
    print("\n-- A refusal that is not about a parameter is reported --")
    forget()

    def handler(body: dict) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})

    try:
        _drive(openai_client, "gpt-4o", handler)
        check("a 401 raises", False, "no exception")
    except ValueError as exc:
        check("a 401 raises with the provider's message",
              "OpenAI API error 401" in str(exc) and "Incorrect API key" in str(exc), str(exc))

    def bad_request(body: dict) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "This model's maximum context length is 8192 tokens."}})

    try:
        seen, _ = _drive(openai_client, "gpt-4o", bad_request)
        check("a 400 about something else raises", False, "no exception")
    except ValueError as exc:
        check("a 400 about something else is not retried",
              "maximum context length" in str(exc), str(exc))
    forget()


def main() -> int:
    print("=" * 52)
    print("  OpenAI-shaped clients")
    print("=" * 52)
    for test in (test_parameter_gate, test_learns_both_parameters, test_known_family_first_time,
                 test_other_providers_retry_too, test_usage_is_normalised,
                 test_an_error_inside_the_stream_surfaces, test_a_real_error_still_surfaces):
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
