"""
test_chroma_client.py — The optional Chroma lookup must not slow the chat.

It sits on the path of every message when configured, and it was two
round trips at four seconds each: a host that dropped packets rather
than refusing delayed every answer by eight seconds (#501). Offline,
with a fake transport:

- the collection id is resolved once and reused, per URL and name
- a stale id (the collection recreated) is forgotten and resolved again
- the whole lookup gives up within its budget and the chat goes on

    python test_chroma_client.py
"""

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-chroma-"))
paths._data_dir_cache = _TEMP

import httpx                                                          # noqa: E402

from backend.ai import chroma_client                                  # noqa: E402

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


COLLECTIONS = "/api/v2/tenants/default_tenant/databases/default_database/collections"


class FakeChroma:
    """Enough of Chroma's HTTP API to count what the client asks it."""

    def __init__(self) -> None:
        self.listed = 0
        self.queried: list[str] = []
        self.collection_id = "id-one"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == COLLECTIONS:
            self.listed += 1
            return httpx.Response(200, json=[{"name": "design_guidelines", "id": self.collection_id}])
        if request.method == "POST" and path.endswith("/query"):
            wanted = path.split("/collections/")[1].split("/")[0]
            self.queried.append(wanted)
            if wanted != self.collection_id:
                return httpx.Response(404, json={"error": "collection not found"})
            return httpx.Response(200, json={
                "documents": [["Use a /31 on point-to-point links."]],
                "metadatas": [[{"source": "addressing.md"}]],
                "distances": [[0.1]],
            })
        return httpx.Response(404, json={"error": f"no route for {path}"})


def _with_fake(handler):
    real_client = httpx.AsyncClient

    class Patched(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    chroma_client.httpx.AsyncClient = Patched
    chroma_client.get_chroma_url = lambda: "http://chroma.test"
    chroma_client.get_chroma_collection = lambda: "design_guidelines"
    chroma_client._COLLECTION_IDS.clear()
    return real_client


def _restore(real_client) -> None:
    chroma_client.httpx.AsyncClient = real_client
    chroma_client._COLLECTION_IDS.clear()


def test_collection_id_is_cached() -> None:
    print("\n-- One listing, many queries --")
    fake = FakeChroma()
    real = _with_fake(fake)
    try:
        async def go():
            first = await chroma_client.query_design_guidelines("point to point addressing")
            second = await chroma_client.query_design_guidelines("loopbacks")
            return first, second
        first, second = asyncio.run(go())
        check("a snippet comes back", first and "/31" in first[0]["text"], str(first))
        check("the collection was listed once for two questions",
              fake.listed == 1 and len(fake.queried) == 2, f"listed {fake.listed}, queried {fake.queried}")
        check("the id is kept per URL and collection name",
              chroma_client._COLLECTION_IDS.get(("http://chroma.test", "design_guidelines")) == "id-one",
              str(chroma_client._COLLECTION_IDS))
    finally:
        _restore(real)


def test_a_stale_id_is_forgotten() -> None:
    print("\n-- The collection was recreated --")
    fake = FakeChroma()
    real = _with_fake(fake)
    try:
        async def go():
            await chroma_client.query_design_guidelines("first")
            fake.collection_id = "id-two"           # recreated under the same name
            missed = await chroma_client.query_design_guidelines("second")
            found = await chroma_client.query_design_guidelines("third")
            return missed, found
        missed, found = asyncio.run(go())
        check("the query against the stale id gives no snippet, not an error", missed is None, str(missed))
        check("  and the stale id is dropped",
              ("http://chroma.test", "design_guidelines") not in chroma_client._COLLECTION_IDS
              or chroma_client._COLLECTION_IDS[("http://chroma.test", "design_guidelines")] == "id-two")
        check("the next question resolves it again and answers",
              found and fake.listed == 2 and fake.queried[-1] == "id-two",
              f"listed {fake.listed}, queried {fake.queried}")
    finally:
        _restore(real)


def test_the_budget() -> None:
    print("\n-- A host that never answers --")

    async def sleepy(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    real = _with_fake(sleepy)
    budget = chroma_client._BUDGET_SECS
    chroma_client._BUDGET_SECS = 0.3
    try:
        started = time.monotonic()
        result = asyncio.run(chroma_client.query_design_guidelines("anything"))
        elapsed = time.monotonic() - started
        check("the lookup gives up within its budget", elapsed < 1.5, f"{elapsed:.1f}s")
        check("  and the chat goes on without a snippet", result is None, str(result))
    finally:
        chroma_client._BUDGET_SECS = budget
        _restore(real)


def test_router_starts_it_first() -> None:
    print("\n-- Overlapped with the preamble --")
    source = Path("backend/ai/router.py").read_text(encoding="utf-8")
    check("the router starts the lookup as a task rather than awaiting it in line",
          "create_task(chroma_client.query_design_guidelines" in source)
    check("  and starts it before the device facts are gathered",
          source.index("create_task(chroma_client") < source.index("to_thread(_device_facts"))


def main() -> int:
    print("=" * 52)
    print("  Chroma lookup")
    print("=" * 52)
    for test in (test_collection_id_is_cached, test_a_stale_id_is_forgotten,
                 test_the_budget, test_router_starts_it_first):
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
