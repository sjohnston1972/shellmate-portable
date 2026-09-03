"""
chroma_client.py — Optional Chroma vector-DB client for ShellMate.

When a Chroma URL is configured (in Settings or via the CHROMA_URL env var)
ShellMate queries the configured collection for design-guideline snippets
relevant to the user's current question and injects them into the AI prompt.

Behaviour:
  - If no URL is configured: skip silently (return None).
  - If the URL is configured but the server is unreachable / returns an error:
    log a warning, return None — never raise into the chat flow.
  - Otherwise return a list of dicts: {"text": "...", "source": "...", "score": float}

Implementation notes:
  - Talks to Chroma's HTTP API directly via httpx so we avoid pulling in the
    full chromadb client library.
  - Uses the v2 API path scheme (Chroma >= 0.5). Falls back to v1 on 404 so
    older deployments still work.
  - Embeds the query using the collection's configured embedding function on
    the server side — we just pass `query_texts` and let Chroma handle it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

from backend.config import CHROMA_URL, CHROMA_COLLECTION
from backend.settings_store import get_effective

logger = logging.getLogger(__name__)

_TIMEOUT_SECS = 4.0    # one request, for the health check
_MAX_RESULTS  = 4
_MAX_CHARS    = 1500   # cap injected context per snippet

#: The whole lookup, on the path of every chat message (#501). Two round
#: trips at four seconds each meant a configured host that dropped packets
#: rather than refusing delayed every answer by eight seconds; a snippet
#: that cannot be had in two is not worth the wait, and the answer goes
#: without it.
_BUDGET_SECS = 2.0

#: Collection ids by (base url, collection name). Chroma's query endpoint
#: wants the id, and listing every collection to find it was one of the
#: two round trips on every message (#501). An entry is dropped when a
#: query answers 404, which is how a collection recreated under the same
#: name shows up.
_COLLECTION_IDS: dict[tuple[str, str], str] = {}


def get_chroma_url() -> str:
    return get_effective("chroma_url", CHROMA_URL)


def get_chroma_collection() -> str:
    val = get_effective("chroma_collection", "")
    return val or os.getenv("CHROMA_COLLECTION") or CHROMA_COLLECTION or "design_guidelines"


def is_configured() -> bool:
    return bool(get_chroma_url())


async def health_check() -> dict:
    """
    Quick reachability test for the configured Chroma server.
    Returns {"ok": bool, "message": str, "url": str}. Never raises.
    """
    url = get_chroma_url()
    if not url:
        return {"ok": False, "message": "Not configured", "url": ""}

    base = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECS) as client:
            for path in ("/api/v2/heartbeat", "/api/v1/heartbeat"):
                r = await client.get(base + path)
                if r.status_code == 200:
                    return {"ok": True, "message": "Connected", "url": base}
            return {"ok": False, "message": f"HTTP {r.status_code}", "url": base}
    except Exception as e:
        return {"ok": False, "message": f"Unreachable: {e}", "url": base}


async def query_design_guidelines(
    query_text: str,
    n_results: int = _MAX_RESULTS,
) -> Optional[list[dict]]:
    """
    Query the configured Chroma collection for snippets relevant to `query_text`.

    Returns None when Chroma isn't configured or the request fails — the
    caller should treat None as "no extra context, carry on as normal".
    """
    url = get_chroma_url()
    if not url:
        return None
    if not query_text or not query_text.strip():
        return None

    collection_name = get_chroma_collection()
    base = url.rstrip("/")

    try:
        return await asyncio.wait_for(
            _lookup(base, collection_name, query_text, n_results), timeout=_BUDGET_SECS)
    except asyncio.TimeoutError:
        logger.warning("Chroma at %s did not answer within %.1fs; answering without it",
                       base, _BUDGET_SECS)
        return None
    except httpx.HTTPError as e:
        logger.warning("Chroma query failed (%s): %s", base, e)
        return None
    except Exception as e:
        logger.warning("Chroma client error: %s", e)
        return None


async def _lookup(base: str, collection_name: str, query_text: str, n_results: int) -> Optional[list[dict]]:
    """Resolve the collection (from cache when possible), then query it."""
    key = (base, collection_name)
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECS) as client:
        collection_id = _COLLECTION_IDS.get(key)
        if not collection_id:
            # Resolve the collection ID (Chroma's query endpoints want UUIDs)
            collection_id = await _resolve_collection_id(client, base, collection_name)
            if not collection_id:
                logger.info("Chroma collection %r not found at %s", collection_name, base)
                return None
            _COLLECTION_IDS[key] = collection_id

        try:
            results = await _query_collection(client, base, collection_id, query_text, n_results)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # The id is stale — the collection was recreated. Forget it,
                # and the next message resolves it afresh.
                _COLLECTION_IDS.pop(key, None)
            raise
        return _format_results(results)


async def _resolve_collection_id(client: httpx.AsyncClient, base: str, name: str) -> str | None:
    """List collections and find the one matching `name`. Tries v2 then v1."""
    # v2 API: GET /api/v2/tenants/default_tenant/databases/default_database/collections
    v2_path = "/api/v2/tenants/default_tenant/databases/default_database/collections"
    try:
        r = await client.get(base + v2_path)
        if r.status_code == 200:
            for c in r.json():
                if c.get("name") == name:
                    return c.get("id")
            return None
    except httpx.HTTPError:
        pass

    # v1 fallback
    r = await client.get(base + "/api/v1/collections")
    r.raise_for_status()
    for c in r.json():
        if c.get("name") == name:
            return c.get("id")
    return None


async def _query_collection(
    client: httpx.AsyncClient,
    base: str,
    collection_id: str,
    query_text: str,
    n_results: int,
) -> dict:
    """POST to the collection's /query endpoint. Tries v2 then v1."""
    body = {
        "query_texts": [query_text],
        "n_results":   n_results,
        "include":     ["documents", "metadatas", "distances"],
    }

    v2_path = (
        f"/api/v2/tenants/default_tenant/databases/default_database"
        f"/collections/{collection_id}/query"
    )
    r = await client.post(base + v2_path, json=body)
    if r.status_code == 200:
        return r.json()

    if r.status_code == 404:
        # v1 fallback
        r = await client.post(base + f"/api/v1/collections/{collection_id}/query", json=body)
        r.raise_for_status()
        return r.json()

    r.raise_for_status()
    return r.json()


def _format_results(raw: dict) -> list[dict]:
    """Flatten Chroma's nested-list response into a list of {text, source, score}."""
    docs       = (raw.get("documents") or [[]])[0]
    metadatas  = (raw.get("metadatas") or [[]])[0]
    distances  = (raw.get("distances") or [[]])[0]
    out: list[dict] = []
    for i, doc in enumerate(docs):
        if not doc:
            continue
        meta  = metadatas[i] if i < len(metadatas) else {}
        score = distances[i] if i < len(distances) else None
        text = doc if len(doc) <= _MAX_CHARS else doc[:_MAX_CHARS] + "…"
        source = ""
        if isinstance(meta, dict):
            source = meta.get("source") or meta.get("title") or meta.get("file") or ""
        out.append({"text": text, "source": source, "score": score})
    return out


def format_for_prompt(snippets: list[dict] | None) -> str:
    """Render snippets as a context block. Empty / None ⇒ empty string."""
    if not snippets:
        return ""
    lines = ["=== DESIGN GUIDELINES (from Chroma DB) ==="]
    for i, s in enumerate(snippets, 1):
        header = f"--- Snippet {i}"
        if s.get("source"):
            header += f" — {s['source']}"
        header += " ---"
        lines.append(header)
        lines.append(s["text"])
        lines.append("")
    return "\n".join(lines)
