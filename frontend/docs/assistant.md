# The AI assistant

The right-hand panel is an assistant that can see your terminal.

It is **off until you ask for it**. A fresh install opens with the terminal at
full width: the assistant is optional, on a locked-down network there may be
no provider to reach at all, and a third of the window given to a pane that
cannot answer anything is a poor introduction to it.

The robot icon in the sidebar turns it on and off. It is the same setting as
**Settings → ShellMate Interface → Show the AI panel** — not a second one that can
disagree with it — and it is remembered between launches.

It lives under ShellMate Interface rather than with the AI settings because it is a
question about the shape of the window rather than about the assistant: it is
the control you want when the terminal needs the full width, which is not a
moment you would think to look under AI.

If you have been using ShellMate already, nothing changes — an existing setup
keeps the panel it has always had.

## Providers

Anthropic, OpenAI, xAI, DeepSeek, or Ollama running locally.

Add a key under **Settings → AI Providers**, then press **Test connections &
refresh models**. The model you pick is remembered between
launches. That does two things: confirms the key works, and rebuilds
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

## The prompts

What the assistant is told before it sees anything of yours lives under
**Settings → AI Prompts**, in full, and is yours to change. Tell it to stop
explaining things you already know, to always quote a change reference, or to
answer in your team's house style.

There is one per mode, and **Reset** restores the shipped text — for one
persona or both.

They are stored as `prompts.json` in your data folder, so they can be edited by
hand, kept in version control, or carried to another machine. Delete the file
and the defaults come back.

### The one thing to leave alone

Each prompt contains `{command_rules}`. That marker is replaced with the
instructions that make suggested commands appear as clickable blocks rather
than as literal tags in the reply.

Move it wherever it reads best. If you delete it, the rules are added at the
end instead — so command suggestions keep working either way. The editor says
so while you are typing rather than after you have saved.

A prompt file that has been broken by hand falls back to the shipped text
rather than leaving the assistant unable to answer.

## Modes

The **Tshoot / Learn** toggle in the chat header switches the assistant's
manner:

- **Troubleshoot** — terse, fix-it-now. Assumes you know what you are doing
  and are in the middle of something.
- **Learn** — explains the why, not just the what.

Whichever you last chose is remembered, so somebody who always wants Learn
says so once.

## Suggested commands

Two switches under **Settings → AI Prompts** govern this: whether the
assistant may suggest commands at all — some people want an explainer and
nothing clickable near a live device — and whether dangerous ones ask first.

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
context, the commands run in them, and the device types.

Terminal output is **cleaned and redacted** first. Cleaning removes escape
sequences and paging artefacts. Redaction masks passwords, hashes, keys and
community strings — the same **Obscure passwords and secrets** setting that
covers session logs, because devices echo and `show run` puts credentials
straight into the buffer. It applies to the Jira export too.

Turn it off and the assistant sees the unmasked truth, which is a reasonable
choice when the model is Ollama running on your own machine and nothing is
leaving it. It is not a reasonable choice with a cloud provider.

Nothing is sent unless you ask a question. The assistant does not watch your
session in the background.

If that is more than you want leaving the machine, use Ollama, or turn the
panel off.
