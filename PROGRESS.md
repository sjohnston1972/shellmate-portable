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

