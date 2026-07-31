# The AI assistant

The right-hand panel is an assistant that can see your terminal. It is
optional — turn it off under **Settings → AI Assistant** and the terminal
takes the full width.

## Providers

Anthropic, OpenAI, xAI, DeepSeek, or Ollama running locally.

Add a key under **Settings → AI Providers**, then press **Test connections &
refresh models**. That does two things: confirms the key works, and rebuilds
the model list from what the provider actually offers — so a model you have
just been granted, or one you have just pulled into Ollama, appears without
anyone editing a list.

Failures are explained rather than reported as a status code. "Rejected the
API key" tells you what to do; `401` does not. A connection failure suggests
the proxy rather than the key, because on a corporate build that is usually
the cause.

### Ollama

Runs locally, needs no key, and nothing leaves the machine. Point ShellMate
at it with the Ollama host setting if it is not on the default port.

Worth considering if the devices you work on are sensitive enough that their
configuration should not go to a third party.

## What it can see

By default, the tab you are looking at: the recent output, the commands you
have run, and which device it is.

The **session picker** in the chat header changes that. Tick several sessions
and the assistant sees all of them — which is what you want for *"why can A
reach C but B cannot"*, where the answer is in the difference between two
devices.

The context indicator in the status bar shows roughly how much of the model's
context window is in use.

## Modes

The **Tshoot / Learn** toggle in the chat header switches the assistant's
manner:

- **Troubleshoot** — terse, fix-it-now. Assumes you know what you are doing
  and are in the middle of something.
- **Learn** — explains the why, not just the what.

## Suggested commands

The assistant can suggest commands, which appear as clickable blocks. Nothing
is sent until you click, and the block names the device it will go to — so a
command suggested while looking at one switch cannot quietly land on another
after you have changed tabs.

Commands it considers dangerous get a confirmation step.

## Knowledge base

If you run a Chroma vector database of your own design guidelines, point
ShellMate at it under **Settings → Knowledge Base**. Matching snippets are
retrieved and added to the assistant's context, so its answers reflect your
standards rather than generic advice.

Left unconfigured, this is skipped entirely with no penalty.

## What is sent

When you ask a question: your message, recent output from the sessions in
context, the commands run in them, and the device types. Terminal output is
cleaned first, so escape sequences and paging artefacts are not sent.

Nothing is sent unless you ask a question. The assistant does not watch your
session in the background.

If that is more than you want leaving the machine, use Ollama, or turn the
panel off.
