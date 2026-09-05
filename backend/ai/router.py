"""
router.py — Routes AI chat requests to the correct backend (Claude / xAI / OpenAI / DeepSeek / Ollama).
Builds context from session buffers, optionally retrieves design-guideline snippets
from a configured Chroma vector DB, and streams the response.
"""
import asyncio
import logging
from collections.abc import AsyncIterator

from backend.ai.prompts import build_context_prompt, build_system_preamble, get_system_prompt
from backend.ai import chroma_client, toolloop, turns
from backend.ai import tools as tool_registry
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


def _ansible_context(canvas: dict | None) -> list[str]:
    """
    What the assistant needs to know about *this* Ansible setup (#602).

    Read from the backend rather than sent up from the browser, because the
    browser would be reporting what it last drew and the question is what is
    actually there. Bounded on purpose: names, not contents. A playbook's
    body belongs in the conversation only when somebody asks about it, and
    an estate of five thousand connections would otherwise fill the window
    before the question did.

    Never raises. An assistant that cannot answer because the runner is
    down is worse than one answering without it — most Ansible questions
    are not about the runner's current mood.
    """
    lines: list[str] = ["=== ShellMate's Ansible integration, right now ==="]
    try:
        from backend import ansible as ansible_module

        state = ansible_module.ping()
        if not state.get("configured"):
            lines.append("Runner: not set up. Settings -> Ansible.")
        elif state.get("reachable") and state.get("authenticated") is not False:
            lines.append(
                f"Runner: connected at {state.get('url', '')}, "
                f"ansible-core {state.get('ansible_core', '?')}, "
                f"{state.get('playbooks', 0)} playbook(s) on it.")
        elif state.get("reachable"):
            lines.append("Runner: reachable but refusing ShellMate — the token "
                         "is wrong or missing.")
        else:
            lines.append(f"Runner: unreachable. {state.get('detail', '')}")
    except Exception:                                     # pragma: no cover
        lines.append("Runner: could not be asked.")

    try:
        from backend import ansible as ansible_module

        mine = [p["name"] for p in ansible_module.library()][:40]
        lines.append("Playbooks written in ShellMate: "
                     + (", ".join(mine) if mine else "none yet"))
    except Exception:                                     # pragma: no cover
        pass

    try:
        from backend import ansible_library, ansible_keys

        templates = [t["name"] for t in ansible_library.templates()][:30]
        envs = [e["name"] for e in ansible_library.environments()][:30]
        keys = [k["name"] for k in ansible_keys.keys()][:30]
        lines.append("Templates: " + (", ".join(templates) or "none"))
        lines.append("Environments: " + (", ".join(envs) or "none"))
        # Names only, ever. The values are in the vault and nothing reads
        # them back — least of all something that leaves the machine.
        lines.append("Key names available to a run: " + (", ".join(keys) or "none"))
    except Exception:                                     # pragma: no cover
        pass

    try:
        from backend import ansible as ansible_module

        inventory = ansible_module.inventory_from_estate("")
        names = sorted(inventory.get("group_names", {}))[:40]
        lines.append(f"Inventory groups Ansible would see ({len(names)}): "
                     + (", ".join(names) or "none"))
        lines.append(f"Reachable hosts in the estate: {len(inventory.get('hosts') or [])}")
        left_out = inventory.get("skipped") or []
        if left_out:
            lines.append(f"{len(left_out)} connection(s) cannot be targeted "
                         "(no address — serial consoles).")
    except Exception:                                     # pragma: no cover
        pass

    # What is on the builder's canvas, which only the browser knows.
    plays = (canvas or {}).get("plays") or []
    if plays:
        lines.append("On the builder's canvas right now:")
        for index, play in enumerate(plays[:10], start=1):
            tasks = play.get("tasks") or []
            lines.append(
                f"  Play {index}: {play.get('name') or 'unnamed'} "
                f"-> targets {play.get('hosts') or 'all'}, "
                f"{len(tasks)} task(s)"
                + (f", {len(play.get('handlers') or [])} handler(s)"
                   if play.get("handlers") else ""))
    return lines


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
    ansible_canvas: dict | None = None,   # the plays on the builder, in Ansible mode
    # An earlier tool exchange in this same turn (#560): the model's own
    # request and the answer to it, rebuilt by the browser after the
    # engineer approved a command. Sent back so the model continues its
    # exchange rather than being told about it afterwards in prose.
    resume: list[dict] | None = None,
    # What the engineer is pointing at (#551): a terminal selection, the
    # last command's output, or something they pasted. Redacted here
    # rather than in the browser — the browser is where the unmasked
    # text already is, and the promise is about what leaves the machine.
    attachment: dict | None = None,
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
    # Optional Chroma-backed design-guideline snippets. Started first and
    # collected last, so its round trips overlap the rest of the preamble —
    # the history reads, the parsing — rather than adding to it (#501).
    # Only queried when a URL is configured; failures are swallowed inside
    # the client, so the task never raises.
    design_task = (asyncio.create_task(chroma_client.query_design_guidelines(message))
                   if chroma_client.is_configured() else None)

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

    active_session = (session_manager.get_session(active_session_id)
                      if active_session_id else None)

    # How much of the session the model can actually see (#553).
    horizon: dict | None = None
    _buffer = (active_session or {}).get("buffer")
    if _buffer is not None and hasattr(_buffer, "horizon"):
        try:
            horizon = _buffer.horizon(int(advanced("ai.context_lines")))
        except Exception:                             # never worth failing chat
            horizon = None

    if active_session_id:
        session = active_session
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

    design_context = ""
    if design_task is not None:
        design_context = chroma_client.format_for_prompt(await design_task)

    effective_mode = (mode or get_settings().get("ai", {}).get("mode") or "tshoot")
    # Ansible mode is chosen by where the user is, not by the mode toggle,
    # so it arrives on the message rather than from settings (#602).
    ansible_block = (await asyncio.to_thread(_ansible_context, ansible_canvas)
                     if effective_mode == "ansible" else None)

    investigation = None
    if effective_mode == "investigate":
        investigation = {"step": int(investigate_step or 0),
                         "max": int(advanced("ai.investigate_max_steps"))}

    context_block = build_context_prompt(
        sessions_summary,
        active_buffer,
        active_label,
        command_history,
        (extra_contexts or [])
        + ([{"label": "Ansible", "buffer": "\n".join(ansible_block)}]
           if ansible_block else []) or None,
        design_context=design_context,
        device_context=device_context,
        parsed_tables=parsed_tables or None,
        investigation=investigation,
        stable_in_system=True,
        horizon=horizon,
    )

    # The exact block this reply rests on, to the browser only (#553).
    # Already redacted — it is the same string the provider gets — so
    # what somebody inspects is what was sent, not a reconstruction of
    # it. Sent before the first chunk so it is on the bubble whether or
    # not the answer finishes.
    # Redacted on the way out, like everything else that leaves (#320).
    attachment_text = ""
    if attachment and str(attachment.get("text") or "").strip():
        attachment_text = turns.attachment_block(
            str(attachment.get("kind") or "selection"),
            outbound.redact_text(str(attachment["text"])))

    yield {"context": (context_block + "\n\n" + attachment_text
                       if attachment_text else context_block)}

    # The same parse, as rows, for the browser (#554). A 48-port table
    # as line-wrapped prose is what the assistant documentation warns
    # models get wrong — and what the engineer gets wrong too. The
    # model keeps the fixed-width text; this is the other half of one
    # parse, not a second one.
    if active_session is not None:
        try:
            from backend.configs import session_platform
            from backend.session import parsed as parsed_module

            table_rows = await asyncio.to_thread(
                parsed_module.rows_for,
                session_platform(active_session),
                active_session.get("recent_records") or [])
            if table_rows:
                yield {"tables": table_rows}
        except Exception as exc:              # never worth failing chat
            logger.debug("Parsed rows unavailable: %s", exc)

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

    # ----------------------------------------------------------------
    # Native tool use (#560)
    #
    # The read-only tools are answered here and the conversation continues
    # without troubling anybody: they reach nothing, so there is nothing to
    # approve. `run_command` is different in kind — it is handed to the
    # browser as a command block, which is exactly what a [SUGGEST_CMD] tag
    # produces today, and the person decides. Their approval sends it and
    # the output comes back on the next request as a tool result, so the
    # model continues its own exchange rather than being told about it
    # afterwards in prose.
    #
    # The bound is `ai.investigate_max_steps`, unchanged and shared with
    # Investigate mode: a model that keeps asking read-only questions must
    # stop somewhere, and the number somebody already tuned is the number.
    # ----------------------------------------------------------------
    tool_defs = None
    shape = "anthropic" if backend == "claude" else "openai"
    if tool_registry.supports(backend, model or ""):
        tool_defs = (tool_registry.for_anthropic() if shape == "anthropic"
                     else tool_registry.for_openai())

    prior: list[dict] = list(resume or [])
    rounds = max(1, int(advanced("ai.investigate_max_steps")))

    for _round in range(rounds):
        said: list[str] = []
        calls: list[dict] = []
        usage_event = None

        async for chunk in stream_response(
            message, context_block, model=model, system_prompt=system_prompt,
            history=history, tools=tool_defs, prior=prior or None,
            attachment=attachment_text,
        ):
            if isinstance(chunk, dict):
                if "tool_calls" in chunk:
                    calls = chunk["tool_calls"]
                    continue
                # Usage is held back until the turn actually ends: yielding
                # it after each round would have the meter count one answer
                # several times.
                usage_event = chunk
                continue
            said.append(chunk)
            yield chunk

        if not calls:
            if usage_event:
                yield usage_event
            return

        answerable, needs_approval = toolloop.partition(calls)

        if needs_approval:
            # The turn stops here. Everything the model said on the way to
            # asking has already been streamed, and the command block the
            # browser renders carries the id, so the result can be tied
            # back to the request when it comes.
            if usage_event:
                yield usage_event
            yield {"tool_request": {
                "shape": shape,
                "text": "".join(said),
                # The read-only calls from the same turn are answered now
                # and travel with the request, so the model does not have
                # to ask for them again after the approval.
                "answered": toolloop.answer_all(answerable, active_session),
                "calls": needs_approval,
                "read_only_calls": answerable,
            }}
            return

        # Only read-only calls: answer them and go round again, without the
        # browser seeing anything but the text.
        results = toolloop.answer_all(answerable, active_session)
        prior = turns.with_tool_exchange(
            prior, shape, "".join(said), answerable, results)

    # The bound was reached. Said rather than left as a turn that simply
    # stopped: a model looping on read-only questions is a fact about the
    # answer somebody is reading.
    yield ("\n\n_The assistant reached its limit of "
           f"{rounds} lookups for this question._")


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

    # What the connect-time drift check found (#549). Read from the cached
    # report, never re-captured: the command has already been sent to the
    # device once, and sending it again to answer a question would be a
    # second, unannounced conversation with it.
    try:
        from backend.ai import explain
        drift = explain.drift_facts(session)
        if drift:
            facts["drift"] = drift
    except Exception:                       # a diff is never worth breaking chat
        pass

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
