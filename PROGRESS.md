# Progress — the AI cluster

Goal: close #560, #553, #551, #554, #556, #557, #559, #558, #555, #552,
#529 and #561.

Sixteen steps in PLAN.md. #560 first because it reshapes the rest; the
largest issues last, so what is left if the run runs out is the largest.
One issue, one close, one push.

Succeeds the estate-cluster run (90 of 90 test files passing at its close).

## 2026-09-05 10:48 — step 1 done (#560)

`backend/ai/tools.py` and the tool-turn shapes in `turns.py`. The
vocabulary, the two payload serialisations, and which models get asked.
Nothing is wired into the clients yet — that is step 2, and splitting it
here keeps a large change testable at each half.

Four tools: `run_command`, and three read-only ones (`get_parsed_output`,
`get_drift`, `search_history`) that are answered from what ShellMate
already holds. The read-only flag is what decides whether a person is
asked, so it is asserted rather than trusted: a "read-only" tool that
opened a channel to go and look would be a device interaction nobody
approved.

**Both payload shapes are generated from one registry.** Anthropic and the
OpenAI-shaped providers describe tools differently and name every piece
differently; written out twice, a tool added to one and not the other is a
model that can ask for something on Claude and not on xAI, with nothing
saying why. The test asserts the two describe the same tools with the same
schemas.

**The two disagree about the result's role**, which is the part most
likely to be got wrong quietly. Anthropic wants every result for one
assistant turn inside a single *user* message — one message each would be
two user turns in a row, which the API refuses. The OpenAI shape wants one
message each with a role of its own, and the arguments as a JSON *string*
rather than an object.

Support is learned, not declared. Ollama's tool support is per model and
per build, so it is off until something says otherwise; a refusal is
remembered so the second request does not pay for it again, and a refusal
overrides a previous success. `ai.native_tools` in Stockton turns the whole
thing off and falls back to tags, which every model supports.

`python test_tools.py` — 49 passed. test_advanced, test_caching and
test_prompts still pass.

## 2026-09-05 10:57 — step 2, first half (#560)

`backend/ai/toolloop.py`: collecting a tool call out of a provider stream,
and answering the read-only ones.

**A real trap, found by writing the test first.** `configs.drift_report` is
exactly what `get_drift` looks like it should call — and it calls
`capture_config`, which opens a second channel to the switch. Built that
way, a model asking "what changed?" would be reaching a device nobody
approved it reaching, at a moment it chose, which is precisely the risk
line on the issue. `get_drift` reads the two most recent *stored*
snapshots instead, and says out loud that it did not capture.

The central test replaces `capture_config`, `capture_config_live`,
`drift_report` and the handler's `send`/`open_secondary_channel` with
things that raise, then calls all three read-only tools. If any of them
reaches a device it fails here rather than in a comms room.

Three smaller decisions:

- **Half a tool call is not a tool call.** Arguments arrive as partial
  JSON; a model that streamed broken JSON has made a mistake it should be
  told about, not one that reaches the engineer as a traceback. Malformed
  arguments become an empty set.
- **An unknown tool name is answered, not dropped.** A silent drop leaves
  the model waiting for a result that never comes; it gets an error naming
  what does exist.
- **"Not run" is a fact, not a reason to run it.** `get_parsed_output` on a
  command nobody ran lists what *was* run and points at `run_command` —
  the path with a person on it. A tool that helpfully ran it would be the
  read-only rule broken by helpfulness.

`python test_toolloop.py` — 34 passed. Next: the client wiring and the
stream_chat loop.

## 2026-09-05 11:29 — step 2, backend complete (#560)

All five clients send `tools` and collect tool calls out of their streams,
and `stream_chat` runs the loop. The browser half is next.

**The rule holds, and the test proves it rather than describing it.** A
`run_command` request *stops the turn*: the model does not get to carry on
as though the command had been run, the browser gets a command block, and
the fake device's handler is a trap that fails the test if anything sends
to it. Two separate tests assert `touched == []`.

Read-only calls do not stop the turn — they are answered and the model
continues, which is the whole point: a question needing drift *and*
history is one turn rather than three requests that each start over. The
test asserts the second request carries the first exchange and the third
carries both, because a model that asked and was ignored looks exactly
like one that answered from nothing.

Four decisions worth recording:

- **The bound is `ai.investigate_max_steps`**, unchanged and shared with
  Investigate mode. A second number to keep in step would drift from it.
  Reaching it is said out loud rather than the turn just ending.
- **Usage is held back until the turn actually ends.** Yielding it per
  round would have the meter count one answer several times.
- **The resume is rebuilt server-side.** The browser says what was asked,
  what came back and which provider shape; the message list is assembled
  in `app.py` so a crafted payload cannot put arbitrary turns in front of
  the model.
- **Ollama returns tool arguments as an object**, where every other
  provider sends a JSON string. Normalised at the client rather than by
  branching inside the collector — one accumulation to be right.

Two existing test fakes enumerated the provider contract and broke when it
grew. Both now take `**kwargs`, with a comment saying why: a fake that
lists every parameter needs editing every time the contract does, and
neither file is about the provider signature.

`python test_tool_turn.py` — 30 passed. `python tools/run_tests.py` —
**93 of 93 test files passed.**

## 2026-09-05 12:07 — #560 closed

The browser half: a `tool_request` renders as the same command block a
suggestion tag produces, and approving it waits for the device, then sends
the output back as the result of the model's own request.

`GET /api/sessions/{id}/last-output` is new. The browser needs the output
of a command it just approved, and the alternative was scraping its own
terminal — which means reimplementing prompt detection in JavaScript
alongside the careful one in `transcript.py`. Its `after` parameter is
what makes it correct rather than merely working: running `show version`
twice would otherwise match the earlier record and hand the model output
from before its own request.

Two changes to chat.js that are better regardless of the test:
`window.shellmateChatMessage` exports the socket entry point (which #558's
conversation restore will replay through), and a tool request arriving with
no live bubble lands on the last assistant one rather than being dropped —
after a reconnect there is nowhere else for it to go.

Declining is a real answer. The model is told and can suggest something
else, rather than waiting for a result that never comes; the block is
struck through and its buttons disabled, because a declined request that
can still be approved is two answers to one question.

**A test of mine was too clever and I corrected the test, not the code.**
`test_chat_context` scans chat.js textually for send sites carrying
`context_session_ids`. The tool resume spreads the remembered payload, so
it carries every field — the heuristic just could not see it. Taught it to
recognise the spread, *and* added an assertion that the remembered payload
itself carries the field, since a spread of a payload that never had it
carries nothing.

`python test_tool_ui.py` — 14 passed. `python tools/run_tests.py` —
**94 of 94 test files passed.**

**#560 is closed.** Next: #553.

## 2026-09-05 12:19 — #553 closed

`SessionBuffer` keeps a timestamp per line and counts evictions, so it can
say where the visible window starts. The heading over the terminal output
now says how many lines are visible, from when, and how many are not —
turning "I cannot see that" from an assertion into a claim about a stated
boundary.

Evicted lines count as hidden alongside lines still in the buffer but
outside the window. Counting only one of the two understates it, and the
number is the whole point.

The block itself reaches the browser as its own event before the first
chunk, so it is on the bubble whether or not the answer finishes. The test
asserts the browser gets **the same string** the provider got, not an
equivalent one: a reconstruction would show what ShellMate believes it
sent, which is precisely what is being questioned.

The inspector counts masked values and says how many. "Redaction is on" is
a claim about a setting; "9 values were masked" is a claim about this
request, and it is the one somebody asked for.

One existing assertion needed updating rather than working around: the
router's yield contract deliberately gained a leading context event, so
test_ai_turns now asserts it comes *first* and that it never arrives as
text in the reply.

`python test_context_inspector.py` — 28 passed. 95 of 95 test files pass.

## 2026-09-05 12:46 — #551 closed

Two terminal menu entries and a paste path, all producing one attachment
shape. The chip sits above the input so what is going with the question is
visible while it is written rather than a surprise after it is sent.

**Three kinds, three headings**, and the paste heading is the one that
matters: it says the text may be from another device or a file. A model
that assumes a pasted configuration is the current session answers
confidently about the wrong switch.

**Redacted on the way out, not in the browser.** The attachment is the one
path into the prompt that is not the session buffer, and it needed the
same door. It also reaches the #553 inspector — a context view that showed
the block and omitted the attachment would be the reconstruction problem in
miniature: most of what the model got, which is worse than none because it
looks complete.

Two mistakes of mine, both worth recording:

- My own two new test fakes enumerated the provider contract and broke on
  `attachment` — the exact thing I had criticised in the pre-existing fakes
  an hour earlier. Both take `**kwargs` now.
- An assertion collided with itself: I checked that "POINTING AT" comes
  before "ENGINEER", and the attachment heading is "THE LINES THE ENGINEER
  IS POINTING AT", so the needle matched inside the thing it was meant to
  precede. Now anchored on "ENGINEER'S QUESTION".

`python test_attachment.py` — 29 passed. 96 of 96 test files pass.

## 2026-09-05 13:12 — #554 closed

`formatText` defers to `markdown.js` — a deletion rather than an addition.
It escapes before producing markup, which is the property that makes it
safe on model output, and this issue asked for that to be checked before
reuse. The tag splitting still runs first, so a command block never
reaches a Markdown parser.

`parsed.rows_for()` gives the browser columns from the same parse the
model's fixed-width text comes from. Sortable numerically where the column
is numbers — sorted as text, 10 comes before 9, which is wrong for exactly
the columns anybody sorts — filterable, and Copy CSV quotes values with
commas in them.

**The best catch was not mine.** `test_outbound` guards that nothing
shapes a record's output without redacting it, and failed on `rows_for`
immediately. It looks over-strict, because these rows go to a browser that
already has the unmasked terminal on screen — but the chat panel is a
different surface, and a conversation gets exported, sent to Jira and
pasted into tickets. A clean table is where a masked value would come back
unmasked. Redacted, with a comment saying why it is not obvious.

Two of my own mistakes: `rows_for` used `outbound` without importing it —
which the source-scanning guard could not catch, because it never executes
the function — and my test used `cisco_ios` as a platform id where
ShellMate's own is `ios`. The wrong id parses nothing, silently, which is
worth knowing.

`python test_chat_tables.py` — 22 passed. 97 of 97 test files pass.

## 2026-09-05 13:31 — #556 closed

Three settings, all defaulting to zero. A budget nobody asked for that
interrupts, or a price ShellMate guessed, would both be worse than the
feature not existing.

The meter takes the **worse** of the context percentage and the budget
percentage — one colour, two things to say with it, and a conversation
inside its window but past its budget is not green.

Two price rates rather than one, because input and output differ by three
to five times on most providers. Nothing shown unless entered, the figure
hedged as "at the rates you entered", and cache reads priced at the input
rate rather than a guessed discount: overstating slightly is honest,
inventing a rate nobody supplied is not.

Asked once at the budget and again at double — a dialog on every message
after the first overrun is one people click through without reading.
Asked *before* anything is drawn, so declining leaves the question in the
box rather than a half-started reply on screen.

Labelled as a budget per conversation in this browser, not per API key,
because one that sounds like a spending cap and is not one is worse than
none.

`python test_budget.py` — 25 passed. 98 of 98 test files pass.

## 2026-09-05 13:41 — #557 closed

`assistant_notes` per platform, into the cached preamble — same from one
question to the next, so it belongs with the persona and costs nothing per
turn on Claude.

**The gate is the feature.** `certain_enough_to_act`, the fingerprint's own
property, carried into the facts rather than re-implemented beside them.
Advice for the wrong platform is worse than none: "prefer set-format
output" on an IOS switch cannot be followed, and a model that follows it
anyway invents a command. The generic profile stays silent — the
never-guess rule applied to advice rather than to commands.

The test asserts `prompts.py` reads the gate and never mentions
`act_threshold`, because a second copy of a threshold is a second thing to
keep in step.

`test_tooltips` caught the tooltip: the natural Junos example contains a
pipe, and `||` is the separator. Reworded rather than escaped.

`python test_platform_notes.py` — 21 passed. 99 of 99 test files pass.

## 2026-09-05 14:01 — #559 closed

Tagged context lines, a citation rule, and chips that scroll the terminal.

**Absolute numbering is the design.** Per-request numbering renumbers every
line whenever anything scrolls, so an older citation points at whatever has
since slid into that slot — wrong exactly when a conversation is long
enough for citations to matter. Numbered by position in the session,
counting evictions; tested by growing a buffer past its own limit.

A chip whose line has scrolled out greys itself rather than scrolling
somewhere arbitrary — highlighting the wrong line looks like an answer.

Wired after the Markdown renderer and never inside a code block: a device
can print something shaped like a citation, and turning that into a chip
would be inventing a reference. Commands are still extracted from the
untagged text, because the extractor matches on the prompt.

`python test_citations.py` — 25 passed. 100 of 100 test files pass.

Also: three read-only survey agents ran in parallel for #558, #555+#561
and #552+#529. Research parallelises here; edits do not — every remaining
issue touches chat.js, router.py, prompts.py and app.py, and my edits are
anchor-based replacements that two writers would break.

## 2026-09-05 14:22 — #558 closed

`chat_messages` with its own FTS5 index, the conversation routes, restore
on load, and export through the report block model rather than a second
renderer.

The raw text is stored, markers and all: `[SUGGEST_CMD]` and `[PLAN]` are
what make a reply a command block and a checklist, so rendered HTML would
keep the appearance and lose the behaviour. Restored replies go back
through `renderBubbleContent`. The session binding cannot come back — those
tabs closed — and the restored conversation says so rather than leaving
stale blocks looking live.

The export unwraps markers instead of printing or stripping them. Left in
they are noise; stripped out the reader loses the suggested commands,
which is most of what a reasoning trail is for.

**Writing this found a real redaction gap, and the fix is partial.** The
redactor masks whatever follows `password` — right for
`username admin password 7 <hash>`, wrong for prose: "the password is
hunter2" masked the word *is* and stored the password. Chat is the first
non-device text to go through that door. The pattern now hops a linking
word, which no platform's configuration writes, so device lines are
unchanged — asserted both ways.

Still partial: "the password for the box is hunter2" gets the wrong token.
The test asserts that failing case explicitly, with a note to invert it
rather than delete it if it is ever fixed. A test that pretended prose was
solved would be worse than one that does not.

One test of mine was timing-fragile: several messages land in one clock
tick on Windows, so asserting a named conversation is not first fails on a
fast machine. Asserts the ordering property now.

`python test_conversations.py` — 54 passed. 101 of 101 test files pass.

## 2026-09-05 14:58 — #552 closed

A fourth persona, a runbook block in the context, `/run <name>`, a Run
with the assistant button, and Save as a runbook on any plan card.

**A persona rather than a flag on Investigate.** Investigate decides what
to do next; a runbook has already been decided, and the two want opposite
things from the model. The prompt forbids reordering, skipping and merging
— with one named exception for a result that makes the remaining steps
unsafe or pointless, which must be announced rather than done quietly.

The step count lives in the browser and moves only where a command is
sent. A server-side count would advance on a proposal and report a step
done that nobody ran.

Save-as-runbook reads the plan card's own elements rather than re-parsing
the reply: the card is what was approved, and anything that renders
differently from what gets saved is a runbook nobody reviewed. Offered on
any plan with commands, because an investigation that found the answer at
step two is a better runbook than one that ran all six.

Also fixed a stale sentence: the editor offered to reset "both prompts".
There were two when that was written and there are five now.

`python test_runbook.py` — 43 passed. 104 of 104 test files pass.

### On the parallel agents

Three ran against fixed contracts, each owning files nobody else touched:
`backend/knowledge.py` (68 tests), `backend/broadcast_collect.py` (55),
and `backend/ollama_pull.py` + the ollama_client changes (36). All three
pass. Integration — routes, Settings, chat.js — stays serial, because
every one of those lands in files all four issues share.

A full-suite run *while* they were writing showed two spurious failures.
Both passed alone. Worth remembering: a suite that reads repo source
cannot be trusted while something else is editing it.

## 2026-09-05 15:31 — a mistake of mine, and the fix

My #552 commit swept two stray `<script src="/static/js/_caching_probe.js">`
tags into `index.html`. They came from `test_caching.py`, which writes a
probe tag, snapshots the file first and restores the snapshot in a
`finally`. The restore assumes nothing else touched the file while it ran —
and I ran the full suite *while editing index.html*, which is the exact
hazard I had described to Steven an hour earlier and then walked into.

Two errors, not one:

- I chained `run_tests.py` and the commit in a single command, so I pushed
  before reading the result. The suite said 103 of 105 and the commit went
  anyway. Never chain a commit behind a suite run.
- `git add -A` while the tree was polluted by a test. The tags were not
  mine and I committed them.

Fixed: the tags are gone, and `test_caching`'s teardown now strips what it
added by pattern rather than restoring a snapshot. A restore can either
clobber a concurrent edit or, as here, carry a stale tag back into the repo
where it surfaces only as a 404 in a different test. Removing what you
added can do neither.

**105 of 105 test files pass** on a clean run with nothing else touching
the tree.

## 2026-09-05 15:56 — #555 closed, and the executable rebuilt

**The exe is current again** — `dist/ShellMate-Portable.exe` at `37904fe`,
carrying four runs of work. ShellMate was not running, so nothing had to be
closed and no live session was at risk; I checked before assuming.

#555: the truncation warning, tokens per second, and pulling a model.

The warning is the point. Ollama cuts a long prompt from the front and
answers confidently about output it never read; the manual documented it
and nothing surfaced it. Reported only when the window is known — with the
setting at 0 no `num_ctx` is sent, so none is invented. Warning on a guess
would train people to ignore it.

Two integration bugs caught before shipping: `lastUsage` is rebuilt field
by field, so `tokens_per_second` was being dropped on its way to the meter;
and a test asserted the pull's phase at an instant after releasing its
gate, a race that passes on an idle machine and fails on a busy one.

`python test_ollama_ergonomics.py` — 49 passed. 105 of 105 test files pass.

