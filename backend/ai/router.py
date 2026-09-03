"""
router.py — Routes AI chat requests to the correct backend (Claude / xAI / OpenAI / DeepSeek / Ollama).
Builds context from session buffers, optionally retrieves design-guideline snippets
from a configured Chroma vector DB, and streams the response.
"""
import asyncio
import logging
from collections.abc import AsyncIterator

from backend.ai.prompts import build_context_prompt, build_system_preamble, get_system_prompt
from backend.ai import chroma_client
from backend.connections.manager import SessionManager
from backend.settings_store import get_settings

from backend.session import outbound
from backend.session.transcript import match_prompt

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

def _extract_commands(buffer_text: str) -> list[str]:
    """
    Pull the commands run in this session out of the terminal text.

    Uses the shared prompt parser rather than a private regex, so Junos,
    PAN-OS and Linux prompts are recognised as well as Cisco ones. This
    previously had its own Cisco-only pattern, which meant the AI was told
    "no commands have been run" on any non-Cisco device.
    """
    commands = []
    for line in buffer_text.splitlines():
        found = match_prompt(line.strip())
        if found and found[1].strip():
            commands.append(found[1].strip())
    return commands


def _session_text(session: dict, lines: int) -> str:
    """
    Return a session's recent output, ready to send to a provider.

    Thin wrapper over :func:`backend.session.outbound.session_text`, which is
    the single place terminal content is prepared to leave the machine — it
    strips escape sequences and masks credentials. Kept as a local name because
    this module calls it from several places.
    """
    return outbound.session_text(session, lines)


async def stream_chat(
    message: str,
    active_session_id: str | None,
    backend: str,
    context_mode: str,                    # "active" | "all" | "1" | "2" etc
    session_manager: SessionManager,
    open_session_ids: list[str] | None = None,  # only sessions the browser has open
    context_session_ids: list[str] | None = None,  # the tab picker's selection
    model: str | None = None,             # optional model override
    mode: str | None = None,              # "learn" | "tshoot"
    history: list[dict] | None = None,    # earlier turns, [{role, text}]
    investigate_step: int | None = None,  # approved steps so far, in Investigate mode
) -> AsyncIterator:
    """
    Build context from session buffers, then stream an AI response.

    Yields text chunks, and — as the last item, when the provider reports
    it — a ``{"usage": {...}}`` dict with the token counts (#416).
    """
    # Filter to sessions the browser currently has open (prevents stale sessions
    # from previous page loads appearing as phantom tabs in the AI context).
    # Crucially, reorder to match the frontend's visual tab order so that
    # tab numbers in the AI context always match what the user sees on screen.
    all_sessions = session_manager.get_all_sessions()
    if open_session_ids:
        id_set = set(open_session_ids)
        session_map = {s.get("session_id"): s for s in all_sessions if s.get("session_id") in id_set}
        # Preserve frontend tab order; skip any IDs the backend no longer has
        all_sessions = [session_map[sid] for sid in open_session_ids if sid in session_map]
    sessions_summary = [
        {
            "tab_num": i + 1,
            "label":   s.get("display_label") or s.get("hostname", "unknown"),
            "hostname": s.get("hostname", "?"),
            "connection_type": s.get("connection_type", "ssh"),
            "session_id": s.get("session_id"),
        }
        for i, s in enumerate(all_sessions)
    ]

    # Active session buffer
    active_label = "No active session"
    active_buffer = "(No terminal session is currently active.)"
    command_history: list[str] = []
    device_context: dict | None = None
    parsed_tables: list[str] = []

    if active_session_id:
        session = session_manager.get_session(active_session_id)
        if session:
            # Both are synchronous work — two history-database reads and
            # up to a dozen TextFSM parses — and this runs inside the chat
            # socket's handler, so they go to a thread rather than holding
            # every other socket while they finish (#496).
            device_context = await asyncio.to_thread(_device_facts, session)
            parsed_tables = await asyncio.to_thread(_parsed_tables, session)
            active_label = (
                session.get("display_label") or
                session.get("hostname", active_session_id[:8])
            )
            if session.get("buffer"):
                active_buffer = _session_text(
                    session, advanced("ai.context_lines"))
                command_history = _extract_commands(active_buffer)

    # Extra contexts (/context all or /context N)
    extra_contexts: list[dict] = []

    if context_mode == "selected":
        # The tab picker's choice arrives in its own field. It used to arrive
        # *as* open_session_ids, which renumbered the OPEN SESSIONS summary
        # over the subset — so tab numbers no longer matched the tab bar and
        # a [SUGGEST_CMD:N] resolved against the wrong tab (#213). The old
        # spelling is still honoured for a page that has not been reloaded.
        chosen = (context_session_ids if context_session_ids is not None
                  else (open_session_ids or []))
        for sid in chosen:
            if sid == active_session_id:
                continue
            sess = session_manager.get_session(sid)
            if sess and sess.get("buffer"):
                extra_contexts.append({
                    "label":  sess.get("display_label") or sess.get("hostname", sid[:8]),
                    "buffer": _session_text(sess, advanced("ai.extra_context_lines")),
                })
    elif context_mode == "all":
        for s in all_sessions:
            sid = s.get("session_id")
            if sid == active_session_id:
                continue
            sess = session_manager.get_session(sid)
            if sess and sess.get("buffer"):
                extra_contexts.append({
                    "label":  sess.get("display_label") or sess.get("hostname", sid[:8]),
                    "buffer": _session_text(sess, advanced("ai.extra_context_lines")),
                })
    elif context_mode.isdigit():
        tab_num = int(context_mode)
        if 1 <= tab_num <= len(all_sessions):
            target = all_sessions[tab_num - 1]
            sid = target.get("session_id")
            if sid and sid != active_session_id:
                sess = session_manager.get_session(sid)
                if sess and sess.get("buffer"):
                    extra_contexts.append({
                        "label":  (
                            target.get("display_label") or
                            target.get("hostname", "")
                        ),
                        "buffer": _session_text(sess, advanced("ai.extra_context_lines")),
                    })

    # Optional Chroma-backed design-guideline snippets. Only queried when a URL
    # is configured (settings or env). Failures are swallowed inside the client.
    design_context = ""
    if chroma_client.is_configured():
        snippets = await chroma_client.query_design_guidelines(message)
        design_context = chroma_client.format_for_prompt(snippets)

    effective_mode = (mode or get_settings().get("ai", {}).get("mode") or "tshoot")
    investigation = None
    if effective_mode == "investigate":
        investigation = {"step": int(investigate_step or 0),
                         "max": int(advanced("ai.investigate_max_steps"))}

    context_block = build_context_prompt(
        sessions_summary,
        active_buffer,
        active_label,
        command_history,
        extra_contexts or None,
        design_context=design_context,
        device_context=device_context,
        parsed_tables=parsed_tables or None,
        investigation=investigation,
        stable_in_system=True,
    )

    # The persona, then the part of the context that holds still between
    # questions — the tab list and the steady device facts — so the cached
    # prefix is long enough to be cached at all (#498). Only what changes
    # travels in the context block above.
    system_prompt = get_system_prompt(effective_mode)
    preamble = build_system_preamble(sessions_summary, active_label, device_context)
    if preamble:
        system_prompt = f"{system_prompt}\n\n{preamble}"

    # Route to the correct backend, passing optional model override + system prompt
    if backend == "claude":
        from backend.ai.claude_client import stream_response
    elif backend == "xai":
        from backend.ai.xai_client import stream_response
    elif backend == "openai":
        from backend.ai.openai_client import stream_response
    elif backend == "deepseek":
        from backend.ai.deepseek_client import stream_response
    else:
        from backend.ai.ollama_client import stream_response

    async for chunk in stream_response(
        message, context_block, model=model, system_prompt=system_prompt,
        history=history,
    ):
        yield chunk


def _parsed_tables(session: dict) -> list[str]:
    """
    Recent show output as rows (#404), for the sessions that keep records.

    The terminal loop appends each finished CommandRecord to the session's
    ``recent_records``; parsing happens here, at question time, so a
    session nobody asks about costs nothing.
    """
    if not advanced("ai.parse_output"):
        return []
    records = session.get("recent_records")
    if not records:
        return []
    try:
        from backend.configs import session_platform
        from backend.session import parsed
        return parsed.tables_for(session_platform(session), records)
    except Exception as exc:                  # parsing must never break chat
        logger.debug("Parsed tables unavailable: %s", exc)
        return []


def _device_facts(session: dict) -> dict:
    """
    What ShellMate has already established about a session's device (#401).

    Read, never fetched: the fingerprint the session carries, the alert
    tracker's pending action, and the archive's record of the last capture.
    Nothing here touches the device, so it is safe on every message.
    Every part is optional and a failure in one leaves the others.
    """
    facts: dict = {"connection_type": session.get("connection_type", "")}

    # The session holds onboard.summarise()'s dict (fingerprint.as_dict()
    # plus the onboarding verdict); a bare Fingerprint is accepted too.
    fp = session.get("fingerprint")
    if fp is not None:
        take = (fp.get if isinstance(fp, dict)
                else lambda key, default="": getattr(fp, key, default))
        facts.update({
            "platform":   take("platform", ""),
            "name":       take("name", ""),
            "version":    take("version", ""),
            "model":      take("model", ""),
            "confidence": take("confidence", 0.0),
            "source":     take("source", ""),
        })

    tracker = session.get("alerts")
    try:
        pending = tracker.payload().get("pending") if tracker else None
    except Exception:                       # a tracker mid-update
        pending = None
    if pending:
        facts["pending"] = pending

    hostname = session.get("hostname") or ""
    if hostname:
        try:
            from backend import config_archive, store
            facts["capture_enabled"] = config_archive.capture_enabled()
            latest = store.store.latest_snapshot(hostname) if hasattr(store, "store") else None
            if latest and latest.get("captured_at"):
                from datetime import datetime
                stamp = latest["captured_at"]
                facts["last_capture"] = (
                    datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
                    if isinstance(stamp, (int, float)) else str(stamp))
                facts["baseline"] = bool(store.store.get_baseline(hostname))
        except Exception:                   # history is never allowed to break chat
            pass
    return facts
