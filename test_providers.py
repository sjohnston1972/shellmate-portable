"""
test_providers.py — Model discovery, and the cache behind the picker.

The model picker used to be only as current as the HTML it was written in,
which is stale the day after it is written (#211). Discovery answers "what can
these providers run right now", and the cache keeps the last successful answer
so a page load shows it without touching the network.

The interesting cases are the unhappy ones: a provider that failed this run
must keep its last known good list — Ollama being stopped for an afternoon
should not erase what it can run — while a provider whose key was removed must
drop out, or the picker goes on offering models nothing can answer with.

    python test_providers.py
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-providers-"))
paths._data_dir_cache = _TEMP

from backend.ai import providers                            # noqa: E402

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


def clear_cache() -> None:
    providers._cache_path().unlink(missing_ok=True)


def result(ok: bool, models: list[dict]) -> dict:
    return {"ok": ok, "message": "", "models": models}


def test_normalise() -> None:
    print("\n-- Normalising provider payloads --")

    anthropic = providers._normalise("anthropic", {
        "data": [
            {"id": "claude-b", "display_name": "Claude B"},
            {"id": "claude-a"},
            {"no_id_here": True},
        ],
    })
    check("anthropic models come back as id and label",
          anthropic == [{"id": "claude-a", "label": "claude-a"},
                        {"id": "claude-b", "label": "Claude B"}],
          repr(anthropic))

    ollama = providers._normalise("ollama", {
        "models": [{"name": "qwen2.5:7b", "details": {"parameter_size": "7B"}}],
    })
    check("ollama models carry their size in the label",
          ollama == [{"id": "qwen2.5:7b", "label": "qwen2.5:7b 7B"}],
          repr(ollama))


def test_cache_round_trip() -> None:
    print("\n-- The cache remembers a good discovery --")
    clear_cache()

    check("no discovery yet means an empty cache", providers.load_cached() == {})

    providers._remember({
        "anthropic": result(True, [{"id": "claude-a", "label": "Claude A"}]),
        "ollama": result(True, [{"id": "qwen2.5:7b", "label": "qwen2.5:7b 7B"}]),
    })
    cached = providers.load_cached()
    check("both providers were kept", sorted(cached) == ["anthropic", "ollama"],
          repr(sorted(cached)))
    check("the models survived the round trip",
          cached["anthropic"]["models"][0]["id"] == "claude-a")


def test_failure_keeps_last_known_good() -> None:
    print("\n-- A failed run does not erase what worked --")
    clear_cache()

    providers._remember({
        "ollama": result(True, [{"id": "qwen2.5:7b", "label": "qwen2.5:7b 7B"}]),
    })
    # Ollama stopped for the afternoon: still configured, but unreachable.
    providers._remember({
        "ollama": result(False, []),
    })
    cached = providers.load_cached()
    check("the last known good list is still there",
          cached.get("ollama", {}).get("models", []) != [],
          repr(cached))


def test_removed_provider_drops_out() -> None:
    print("\n-- A removed provider stops being offered --")
    clear_cache()

    providers._remember({
        "anthropic": result(True, [{"id": "claude-a", "label": "Claude A"}]),
        "openai": result(True, [{"id": "gpt-x", "label": "gpt-x"}]),
    })
    # The OpenAI key was deleted, so check_all no longer includes it at all.
    providers._remember({
        "anthropic": result(True, [{"id": "claude-a", "label": "Claude A"}]),
    })
    cached = providers.load_cached()
    check("the unconfigured provider is gone", "openai" not in cached,
          repr(sorted(cached)))
    check("the remaining one stayed", "anthropic" in cached)


def test_unreadable_cache_is_just_empty() -> None:
    print("\n-- A corrupt cache degrades, never breaks --")
    providers._cache_path().write_text("not json {", encoding="utf-8")
    check("garbage reads as an empty cache", providers.load_cached() == {})

    providers._cache_path().write_text(json.dumps(["a", "list"]), encoding="utf-8")
    check("the wrong shape reads as an empty cache", providers.load_cached() == {})


def test_cached_endpoint() -> None:
    print("\n-- The endpoint the picker loads from --")
    from fastapi.testclient import TestClient
    from backend.app import app

    clear_cache()
    providers._remember({
        "anthropic": result(True, [{"id": "claude-a", "label": "Claude A"}]),
    })

    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.get("/api/providers/cached")
    check("the cache is served", response.status_code == 200, response.text)
    check("and holds what was remembered",
          "anthropic" in response.json(), response.text)


def test_claude_fallback_resolves_from_cache() -> None:
    print("\n-- The no-model fallback follows discovery --")
    from backend.ai import claude_client

    clear_cache()
    check("with no discovery the constant applies",
          claude_client._fallback_model() == claude_client.MODEL)

    providers._remember({
        "anthropic": result(True, [{"id": "claude-current", "label": "Claude"}]),
    })
    check("with a discovery the discovered model wins (#230)",
          claude_client._fallback_model() == "claude-current",
          claude_client._fallback_model())


def test_picker_wiring() -> None:
    """
    The frontend half, read out of the source.

    The failure is invisible at runtime — a picker that never asks for the
    cache simply keeps showing the hardcoded list, which is exactly the bug
    this exists to prevent.
    """
    print("\n-- The picker is wired to the cache --")
    root = Path(__file__).parent / "frontend"

    panel = (root / "js" / "ai_panel.js").read_text(encoding="utf-8")
    check("the panel asks for the cached list on load",
          "/api/providers/cached" in panel and "restoreCachedModels()" in panel)
    check("a rebuild keeps the local group's anchor",
          re.search(r"local\.id\s*=\s*'local-models-group'", panel) is not None,
          "chat.js finds the group by id; a rebuild that drops it strands "
          "the live Ollama refresh")

    html = (root / "index.html").read_text(encoding="utf-8")
    check("the refresh action exists beside the picker",
          'id="btn-refresh-models"' in html)

    chat = (root / "js" / "chat.js").read_text(encoding="utf-8")
    check("the local list is re-asked after every rebuild",
          "shellmate:models-refreshed" in chat and "loadLocalModels" in chat)


def main() -> int:
    print("\n" + "=" * 52)
    print("  Providers")
    print("=" * 52)

    for test in (
        test_normalise,
        test_cache_round_trip,
        test_failure_keeps_last_known_good,
        test_removed_provider_drops_out,
        test_unreadable_cache_is_just_empty,
        test_cached_endpoint,
        test_claude_fallback_resolves_from_cache,
        test_picker_wiring,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__} raised {exc!r}")
            print(f"  FAIL {test.__name__} raised\n       {exc!r}")

    print("\n" + "-" * 52)
    if failed:
        print(f"  {passed} passed, {len(failed)} FAILED")
        for line in failed:
            print(f"    - {line}")
    else:
        print(f"  all {passed} checks passed")
    print("-" * 52)

    shutil.rmtree(_TEMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
