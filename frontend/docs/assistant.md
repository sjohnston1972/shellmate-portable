# The AI assistant

The right-hand panel is an assistant that can see your terminal.

It is **off until you ask for it**. A fresh install opens with the terminal at
full width: the assistant is optional, on a locked-down network there may be
no provider to reach at all, and a third of the window given to a pane that
cannot answer anything is a poor introduction to it.

The robot icon in the **tab bar**, beside the layout button, turns it on and
off. It sits there rather than in the sidebar because the assistant is half of
the split screen, not a panel that opens on top — it belongs with the control
that decides what the window is showing. It is the same setting as
**Settings → ShellMate Interface → Show the AI panel** — not a second one that
can disagree with it — and it is remembered between launches.

If you have been using ShellMate already, nothing changes — an existing setup
keeps the panel it has always had.

## Providers

Anthropic, OpenAI, xAI, DeepSeek, or Ollama running locally.

Add a key under **Settings → AI Providers**, then press **Test connections &
refresh models**. That does two things: confirms the key works, and rebuilds
the model list from what each provider actually offers — every provider is
asked for its own list, so a model you have just been granted, or one you have
just pulled into Ollama, appears without anyone editing anything.

Failures are explained rather than reported as a status code. "Rejected the
API key" tells you what to do; `401` does not. A connection failure suggests
the proxy rather than the key, because on a corporate build that is usually
the cause.

### Picking a model

The dropdown in the chat header holds every model discovered, cloud providers
and local Ollama grouped separately, with a refresh button beside it — the
same discovery as the Settings button, reachable from where a stale list gets
noticed.

What discovery finds is kept — `models.json` in your data folder — and fills
the picker on every page load, so the list survives a restart without touching
the network. Discovery also re-runs on its own when you save a provider key,
because a new key changes what is on offer, and when a provider rejects a
retired model id — so a model withdrawn from under you drops out of the picker
instead of lingering as an option that only ever errors.

The model you pick is saved to `settings.json`, travels with the data folder,
and is back on the next launch.

### Ollama

Runs locally, needs no key, and nothing leaves the machine. Point ShellMate
at it with the Ollama host setting if it is not on the default port.

Worth considering if the devices you work on are sensitive enough that their
configuration should not go to a third party.

A local model has two more settings than a cloud one, under Stockton → AI
Assistant: **Ollama context window** (`num_ctx`), which defaults to 8192
because Ollama's own default of 2048 is overrun by a two-hundred-line buffer
without a word of warning — the model simply never sees the start — and
**Keep the local model loaded**, how long Ollama keeps the model in memory
after a reply so the next question does not pay the load again. Temperature
and the reply ceiling apply to Ollama exactly as they do to the cloud
providers.

## What it can see

By default, the tab you are looking at: the recent output, the commands you
have run, and which device it is.

The **session picker** in the chat header changes that. **Follow the active
tab** is the default; **Choose sessions** lets you tick several, and the
assistant sees all of them — which is what you want for *"why can A reach C
but B cannot"*, where the answer is in the difference between two devices.

The choice travels with **every** request, including the automatic analysis
after an approved command, until you change it or the tabs close. And it is
visible: the indicator in the chat header reads *N sessions* while a choice is
in force, and the context meter in the status bar counts the added buffers —
what the assistant will see is on screen, not remembered.

That meter estimates how much of the model's context window the next question
will use — green under a quarter, red when it is worth trimming.

## Modes

The mode button in the chat header cycles through three ways of working:

- **Troubleshoot** — terse, fix-it-now. Assumes you know what you are doing
  and are in the middle of something.
- **Learn** — explains the why, not just the what.
- **Investigate** — runs a problem down to a plan, one approved step at a
  time. See below.

Whichever you last chose is remembered, so somebody who always wants Learn
says so once.

### Investigate

Describe the problem — or type `/investigate` followed by it — and the
assistant states a hypothesis, lays out a short plan of read-only steps as a
checklist, and proposes only the first as a command. You approve it, as with
any suggestion; the output comes back to the assistant, which says what it
learned, ticks or drops steps, and proposes the next. It finishes with a
**Conclusion** that names the cause and the evidence, and puts any
configuration change there, flagged, never as a step.

Nothing runs without your click. The plan is on screen throughout, so you
can see where it is going and stop it. There is a **step budget** — eight
approved commands by default, under Stockton → AI Assistant — which the
assistant is told about and plans within; once it is spent, results are no
longer fed back and you ask for a conclusion or clear the chat.

A run looks like this. You type `/investigate users on the third floor say
the network is slow`. The assistant replies with a hypothesis — *an access
uplink is saturated or erroring* — and a plan card:

1. ☐ `show interfaces status` — which uplinks are up and at what speed
2. ☐ `show interfaces | include line protocol|input errors|CRC` — errors
3. ☐ `show interfaces counters` — utilisation

and one command block, the first step. You send it; the output comes back to
the assistant on its own, as it does after any approved command; the reply
says what it showed — *Gi1/0/48 is up at 100 Mb/s, the rest at 1 Gb/s* —
ticks step one, and proposes step two. When the cause is established it
finishes with **Conclusion**: what is wrong, the evidence, and what to do,
with any configuration change flagged as a recommendation rather than a
step. Switching mode, or clearing the chat, ends the run and resets the
step count.

## Structured output

Where ntc-templates has a template for a show command — most of what anyone
types on IOS, NX-OS, ASA, Junos, PAN-OS, EOS and Linux — the output of your
recent commands also reaches the assistant as rows, parsed locally. A
48-port interface table as columns is how a model gets a port wrong; as rows
it does not. The raw text still goes too, because a template can lag a
release. **Parse show output into tables** in Stockton switches it off.

The rows come from the three most recent commands that have a template, and
from the output of an approved command when it is fed back. A command with
no template is simply not parsed — there is no error and nothing changes. A
long table is cut at sixty rows with a note; the raw output carries the rest.
Parsing runs on the redacted text, so nothing masked from the raw output
reappears in the rows.

## Suggested commands

The assistant can suggest commands, which appear as clickable blocks. Nothing
is sent until you press **Send**, and **Edit** lets you reword one first. Each
block names the tab it will go to — so a command suggested while looking at
one switch cannot quietly land on another after you have changed tabs; sending
it switches you to that tab so you see it arrive.

Commands the platform lists as dangerous — `reload`, `write erase` and their
relatives — are held for confirmation before they reach the device, by the
same guardrail that catches a `write erase` you type yourself. Which commands
count is per-platform, under Platform Definitions.

Two switches under **Settings → AI Assistant** govern this: whether the
assistant may suggest commands at all — some people want an explainer and
nothing clickable near a live device — and whether dangerous ones ask first.

### After you approve one

When a suggested command runs, the device's reply is gathered and sent back to
the assistant, which comments on it unprompted — approve `show ip bgp summary`
and the next message is about your neighbours. Sending device output to your
provider is a different decision from running the command, so it has its own
switch — **Comment on output after an approved command**, in Settings → AI
Assistant — and its own cap on how many lines are shipped. Off, commands still
run and are still suggested; you just ask about the result yourself.

## The prompts

What the assistant is told before it sees anything of yours lives under
**Settings → AI Assistant**, in full, and is yours to change. Tell it to stop
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

## Knowledge base

If you run a Chroma vector database of your own design guidelines, point
ShellMate at it under **Settings → AI Providers**, in the **Knowledge Base
(Chroma DB)** subsection — there is a Test connection button next to the
fields. Matching snippets are retrieved on every chat and added to the
assistant's context, so its answers reflect your standards rather than generic
advice.

Left unconfigured, this is skipped entirely with no penalty.

## Around the conversation

**Quick buttons** under the messages ask a stock question in one press. They
are yours to change: right-click one to reword it, **+** to add one, **×** to
remove it. The set is kept in `settings.json`, so a curated row travels with
the data folder.

**Pop out** floats the chat over the terminal — drag the header to move it,
the corner to resize — for when the terminal has the whole width but a
question comes up anyway.

**Enter** sends by default, with Shift+Enter for a new line. If you write
paragraphs rather than sentences, **Settings → ShellMate Interface → Enter in
the chat box** swaps that round: Enter starts a new line and Ctrl+Enter sends.

**Clear** empties the conversation. **Conclude** does the opposite of
forgetting: it summarises the sessions and the chat, and logs the result to
Jira — the write-up your work order was going to need anyway.

## What is sent

When you ask a question: your message, recent output from the sessions in
context, the commands run in them, the device types, and any knowledge-base
snippets that matched.

Terminal output is **cleaned and redacted** first. Cleaning removes escape
sequences and paging artefacts. Redaction masks passwords, hashes, keys and
community strings — the same **Obscure passwords and secrets** setting that
covers session logs, because devices echo and `show run` puts credentials
straight into the buffer. It applies to the Jira export too, and to the
automatic analysis after an approved command, whose prompt is composed on the
server precisely so the masking sees the output before any provider does.

Turn it off and the assistant sees the unmasked truth, which is a reasonable
choice when the model is Ollama running on your own machine and nothing is
leaving it. It is not a reasonable choice with a cloud provider.

Nothing is sent unless you ask a question — or approve a suggested command
while the after-approval analysis is on, which is the one case where the
assistant speaks without being spoken to, and has its own switch for exactly
that reason. The assistant does not watch your sessions in the background.

If that is more than you want leaving the machine, use Ollama, or turn the
panel off.


## What it knows without being told

Every question carries, besides the terminal output, what ShellMate has
already established about the active device: the platform and version from
the fingerprint, how sure it is and how it found out, anything pending on the
device — a reload counting down, a commit waiting to be confirmed — and when
the configuration was last captured. The assistant used to guess the vendor
from the shape of the prompt while the application knew the answer.

It also carries **what changed since your last visit**, when the connect-time
drift check found anything: the diff itself, masked and capped, labelled as
something ShellMate captured rather than something you typed. "What changed
since yesterday" is the first question in most outages and the answer was
already in the archive; until now the assistant reconstructed it from whatever
`show run` happened to be in the buffer. The cap is **Lines of the
configuration diff sent** in Stockton, under AI Assistant — zero sends none of
it.

## Memory

The last few exchanges travel with each request, so "and the other
interface?" means what you think it means. How many is **Earlier turns the
assistant remembers** in Stockton, under AI Assistant; zero switches memory
off. Clearing the chat clears it.

On Claude the system prompt and those earlier turns are marked as a cacheable
prefix, so each request re-reads the conversation from cache and pays full
price only for the fresh terminal context and the new question. **Cache the
conversation prefix** in Stockton is the switch, on by default; other
providers ignore it.

Two details make that prefix worth caching. The tab list and what ShellMate
has established about the device travel with the system prompt rather than
with each question, because they are the same from one question to the next
and the persona alone is shorter than the provider's minimum for a cached
prefix. And once a conversation is over the remembered-turns limit, the
oldest turns are dropped four at a time rather than one per request, so the
cached part stays the same for several questions running — a prefix that
changed on every request was written to the cache each time and never read
back. The memory therefore sits between the limit and four turns below it.

## The context meter

The status bar's **Context** figure is an estimate until the provider has
answered once; after that it is the provider's own count of what the last
request contained, and the tooltip shows tokens in and out for the last reply
and for the whole conversation, with how much came from the cache. Prices are
not shown on purpose: they change without notice and a wrong figure is worse
than none.
