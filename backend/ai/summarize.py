"""
summarize.py — One-shot AI summary of a terminal session for the
"Session notes" field of the Conclude-Session (Jira) modal.

Reuses the streaming AI clients but collects the chunks into a single
string before returning.
"""
from __future__ import annotations

import logging
from typing import Iterable

from backend.connections.manager import SessionManager
from backend.session import outbound

logger = logging.getLogger(__name__)


#: The persona for writing notes. Not the troubleshoot prompt (#502): that
#: one tells the model to suggest one command at a time in [SUGGEST_CMD]
#: tags and to flag dangerous ones, and notes written under it arrived
#: with command blocks and warnings in them — on their way into a ticket.
NOTES_SYSTEM_PROMPT = (
    "You are a senior network engineer writing up a shell session for a "
    "ticket. You write plain, factual notes about what was done and what "
    "was found, in prose. You do not suggest commands, do not use tags or "
    "markup of any kind, do not address the reader, and do not add anything "
    "that is not in the material you are given."
)

_TASK_INSTRUCTIONS = (
    "TASK: Write post-session notes for a network-engineering shell session.\n"
    "Read the terminal transcripts and chat history below, then produce a "
    "concise factual summary in plain text (no markdown headings, no bullet "
    "lists unless essential). Cover, in order:\n"
    "  1. What was investigated or worked on.\n"
    "  2. Key findings or evidence (specific values, errors, interface "
    "names, IPs).\n"
    "  3. Actions taken and the outcome.\n"
    "Keep it 3–6 short sentences. Do not invent facts. Output only the notes "
    "themselves, no preamble. If the transcripts are all empty, return: "
    "'No terminal activity captured.'"
)


def _build_user_prompt(
    transcripts: list[dict],
    chat_messages: list[dict] | None,
) -> str:
    parts: list[str] = [_TASK_INSTRUCTIONS, ""]
    if transcripts:
        for t in transcripts:
            parts.append(f"=== Tab: {t['label']} ({t['hostname']}) ===")
            parts.append(t["buffer"] or "(empty buffer)")
    else:
        parts.append("(No terminal sessions were captured.)")

    if chat_messages:
        parts.append("\n=== Chat history (user ↔ AI) ===")
        for m in chat_messages:
            role = m.get("role", "user")
            # Redacted like the buffers above (#320): the chat quotes device
            # output constantly, and this text leaves the machine for a
            # third-party API.
            text = outbound.redact_text((m.get("text") or "").strip())
            if text:
                parts.append(f"[{role}] {text}")

    parts.append("\nWrite the session notes now.")
    return "\n".join(parts)


async def summarize_session(
    open_session_ids: list[str],
    chat_messages: list[dict] | None,
    backend: str,
    session_manager: SessionManager,
    model: str | None = None,
) -> str:
    """
    Build a transcript from the given sessions, ask the AI for a short
    summary, and return the full text.
    """
    transcripts: list[dict] = []
    for sid in (open_session_ids or []):
        sess = session_manager.get_session(sid)
        if not sess or not sess.get("buffer"):
            continue
        transcripts.append({
            "label":    sess.get("display_label") or sess.get("hostname", sid[:8]),
            "hostname": sess.get("hostname", "?"),
            # Through the outbound helper rather than straight off the
            # buffer: this text goes to a third-party API, and devices echo.
            "buffer":   outbound.session_text(sess, 400),
        })

    user_prompt = _build_user_prompt(transcripts, chat_messages)

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

    # The streaming clients take (user_message, context_block). The task
    # goes as the message with no context block — the transcripts are in
    # the task itself — and under the note-writing persona rather than the
    # clients' default, which is the troubleshoot one (#502).
    chunks: list[str] = []
    async for chunk in stream_response(user_prompt, "", model=model,
                                       system_prompt=NOTES_SYSTEM_PROMPT):
        if isinstance(chunk, str):
            chunks.append(chunk)
    return "".join(chunks).strip()
