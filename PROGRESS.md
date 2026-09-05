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

