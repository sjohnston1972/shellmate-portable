"""
http.py — One httpx client per provider, reused across requests (#503).

Every chat turn and every auto-analysis turn used to open a fresh
``httpx.AsyncClient`` — a new connection pool and a new TLS handshake for
each — in all six places the assistant talks to something. A client is
now created on first use and kept, keyed on the provider, the timeout it
was asked for, and the event loop it was created on (an async client is
bound to its loop, and the tests run several). ``aclose_all()`` runs at
application shutdown.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_clients: dict[tuple, httpx.AsyncClient] = {}


def shared(name: str, timeout) -> httpx.AsyncClient:
    """The client for *name* with this *timeout*, on the running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    key = (name, repr(timeout), id(loop), httpx.AsyncClient)
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout)
        _clients[key] = client
    return client


async def aclose_all() -> None:
    """Close every client. Safe to call more than once."""
    for key, client in list(_clients.items()):
        _clients.pop(key, None)
        try:
            await client.aclose()
        except Exception as exc:                      # a loop that is already gone
            logger.debug("Closing the %s client: %s", key[0], exc)
