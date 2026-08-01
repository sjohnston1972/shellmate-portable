"""
router.py — Routes AI chat requests to the correct backend (Claude / xAI / OpenAI / DeepSeek / Ollama).
Builds context from session buffers, optionally retrieves design-guideline snippets
from a configured Chroma vector DB, and streams the response.
"""
import logging
from collections.abc import AsyncIterator

from backend.ai.prompts import build_context_prompt, get_system_prompt
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
    model: str | None = None,             # optional model override
    mode: str | None = None,              # "learn" | "tshoot"
) -> AsyncIterator[str]:
    """
    Build context from session buffers, then stream an AI response.
    Yields text chunks.
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

    if active_session_id:
        session = session_manager.get_session(active_session_id)
        if session:
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
        # The tab picker sends exactly the sessions it wants included, so
        # open_session_ids is the selection rather than "everything open".
        for sid in (open_session_ids or []):
            if sid == active_session_id:
                continue
            sess = session_manager.get_session(sid)
            if sess and sess.get("buffer"):
                extra_contexts.append({
                    "label":  sess.get("display_label") or sess.get("hostname", sid[:8]),
                    "buffer": _session_text(sess, 100),
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
                    "buffer": _session_text(sess, 100),
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
                        "buffer": _session_text(sess, 100),
                    })

    # Optional Chroma-backed design-guideline snippets. Only queried when a URL
    # is configured (settings or env). Failures are swallowed inside the client.
    design_context = ""
    if chroma_client.is_configured():
        snippets = await chroma_client.query_design_guidelines(message)
        design_context = chroma_client.format_for_prompt(snippets)

    context_block = build_context_prompt(
        sessions_summary,
        active_buffer,
        active_label,
        command_history,
        extra_contexts or None,
        design_context=design_context,
    )

    # Pick persona from explicit mode arg, falling back to stored preference
    effective_mode = (mode or get_settings().get("ai", {}).get("mode") or "tshoot")
    system_prompt = get_system_prompt(effective_mode)

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
    ):
        yield chunk
