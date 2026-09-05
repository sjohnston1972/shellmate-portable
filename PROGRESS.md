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

