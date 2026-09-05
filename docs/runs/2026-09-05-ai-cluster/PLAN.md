# Run plan — the AI cluster

Twelve issues: #560, #553, #551, #554, #556, #557, #559, #558, #555, #552,
#529, #561.

Succeeds the estate-cluster run, archived in
`docs/runs/2026-09-05-estate-cluster/` with its own close-out entry. No DONE
was written for it: it is being succeeded immediately rather than handed
over, and a DONE created and deleted in the same minute is a signal nobody
can read. #544 and #543 are closed on GitHub; #547 was never started and
goes back on the open list untouched.

## The one decision taken here

**#560 goes first.** It changes the shape of every other AI issue —
citations, the context inspector, runbooks and the broadcast comparison all
look different once the model can ask for what it needs instead of being
pre-loaded with it. Doing it late means doing several of them twice. This
was flagged at the end of the last two runs and not overruled, so it is
taken as settled; say so if it should not be.

## How this run commits

One issue, one close, one push — as asked. A step that finishes an issue
closes it on GitHub with what shipped and why, then commits and pushes.
Steps that are half of an issue commit and push without closing.

## Ordering

Largest last. #561 is medium-to-large and #529 and #552 are mediums; if
the run runs out it stops there, and the smaller issues that compound with
#560 are done either way.

---

## #560 — native tool use

### 1. The tool shapes, per provider

`turns.py` grows a tool-message shape for each of the five providers, and
each client learns to send `tools` and to stream tool blocks. Claude,
OpenAI, xAI and DeepSeek are three payload shapes between them; Ollama's
support is uneven and is probed rather than assumed.

**Done:** `python test_turns.py` (new if absent) asserts each provider's
tool call and tool result round-trip, and that a model with no tool support
falls back to tags.

### 2. The loop, with the approval gate untouched

The socket gains `tool_call` and `tool_result`. A `run_command` call
renders as today's command block; approving it sends it and returns the
output as a tool result so the model continues the same turn. The
guardrail in `pipeline.py`, the approval, and `investigate_max_steps` stay
exactly where they are.

**Done:** a test drives a whole tool turn against a fake provider and
proves nothing reaches a device without approval.

### 3. The read-only tools

`get_parsed_output(command)`, `get_drift()`, `search_history(query)` —
answered from what ShellMate already holds, needing no approval because
they touch no device.

**Done:** a test proves each is answered without a session write, and that
a read-only tool asking for something absent says so rather than inventing.

**Closes #560.**

## 4. #553 — show what the assistant saw

The exact redacted context block each reply carried, on the bubble, plus a
line saying where the visible window starts. Done next because it is what
makes everything above inspectable.

**Closes #553.**

## 5. #551 — point at the screen

Ask about a selection; explain the last command's output; a pasted config
as the same attachment type, redacted server-side.

**Closes #551.**

## 6. #554 — real tables and proper Markdown

`markdown.js` for AI bubbles, parsed rows shipped to the browser as a real
table with Copy CSV. The `[SUGGEST_CMD]` and `[PLAN]` splitting stays ahead
of rendering, and the escaping is checked before reuse on model output.

**Closes #554.**

## 7. #556 — a token budget

Three bounded Stockton settings, the meter against them, and a price the
team enters rather than one ShellMate guesses.

**Closes #556.**

## 8. #557 — per-platform assistant notes

`assistant_notes` per platform, appended to the cached preamble only when
the fingerprint is certain enough. The generic profile stays silent.

**Closes #557.**

## 9. #559 — citations

Numbered context lines, a rule to cite them, chips that scroll the terminal
and highlight. Where the model does not cite, nothing changes.

**Closes #559.**

## 10-11. #558 — conversation persistence and export

Saved through the same redaction as the summary path, restored on relaunch,
searchable from History, exported as Markdown. Two steps: the store and the
API, then the restore and the export.

**Closes #558.**

## 12. #555 — local-model ergonomics

Pull a model with progress, tokens per second, and a warning when the
prompt filled the context — the silent-truncation problem the manual
documents and nothing surfaces.

**Closes #555.**

## 13. #552 — runbooks

A snippet becomes a plan the assistant walks with approval on every step,
and a finished Investigate plan can be saved back as a snippet. A fourth
persona body, which `prompts_editor.js` must show.

**Closes #552.**

## 14. #529 — broadcast that compares

Collect the replies, diff each device against the first, and hand the
parsed rows to the assistant rather than a hundred raw lines each.

**Closes #529.**

## 15. #561 — a local knowledge folder

`ShellMate-Data/knowledge/` indexed into FTS5 and injected through the same
block as Chroma, with redaction over the snippets. Last because it is the
largest and the only one that stands alone.

**Closes #561.**

## 16. Close the run

Regenerate the tree, run the whole suite, archive, write DONE.

**Done:** `python tools/run_tests.py` reports every file passing.

---

## Not in this run

- **The executable is still not rebuilt**, for the third run running. It
  cannot be while ShellMate is running.
- **#547** goes back on the list untouched.
- If the run runs out it stops inside whichever issue it is on and DONE
  says which — the issues are ordered so that what is left is the largest.
