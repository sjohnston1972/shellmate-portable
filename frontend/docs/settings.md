# Settings

Open **Settings** from the sidebar. It is a category list with a search box:
if you know roughly what a setting is called, type it and every matching row
across every category appears, whichever one it lives under.

Several rows carry a **?** beside the label. That is where the trade-off is
written — what the setting does is usually clear from its name, what you give
up by turning it on is not. `Copy on Select` explains itself; that it will
replace your clipboard every time you drag across the terminal does not.

Everything is stored in `settings.json` in your data folder, and nothing
sensitive goes in it — API keys are diverted into the vault before the file is
written.

## The categories

| Category | What lives there |
|---|---|
| Terminal Appearance | Font, size, colour scheme, cursor, and a live preview |
| ShellMate Interface | Theme, text size, density, tab width, confirmations, window behaviour, whether the AI panel is shown |
| Alerts | How loudly a pending reload or commit-confirm interrupts you |
| Behavior | Scrollback, right-click paste, copy on select |
| Device Awareness | Paging-off and alias expansion, both gated on confident identification |
| Platform Definitions | What ShellMate knows about each kind of device |
| Serial Defaults | Baud rate and framing for new serial connections |
| Output Colours | Regular expressions that colour terminal text |
| Credentials Vault | How saved passwords and API keys are encrypted |
| AI Prompts | What the assistant is told, and whether it may suggest commands |
| AI Providers | API keys, endpoints, and the connection test |
| Knowledge Base | An optional Chroma collection of your own guidelines |
| Session Logging | Plain-text logs, and whether secrets are masked in them |
| Configuration Capture | Snapshots on connect, the diff prompt, and the file archive |

## The ones worth knowing about

### The assistant is off until you ask for it

A fresh install opens with the terminal at full width. The assistant is
optional, on a locked-down network there may be no provider to reach at all,
and a third of the window given to a pane that cannot answer anything is a
poor first impression.

The robot icon in the sidebar turns it on and off. It is the same setting as
**ShellMate Interface → Show the AI panel**, not a second one that can disagree with it.
It sits under ShellMate Interface rather than with the AI settings because it decides
the shape of the window, not how the assistant behaves.

An existing installation is left exactly as it was. A default is only a
default for a setup that has never been configured — changing one under
somebody who has been using the feature for months would be a change they did
not make and cannot explain.

### Device Awareness depends on confidence

Both switches in that section — paging-off and alias expansion — do nothing
unless the device was identified *confidently*. That is the most important
thing about the section, and it is common for it not to be met: a legal banner
in place of a version string leaves ShellMate identifying the device from its
prompt alone, which is not enough to act on.

When that happens the note in the corner of the terminal says so, and names
the command it declined to send. Click the device name in the status bar to
say what it is yourself.

See [Device awareness](#device-awareness).

### Redaction covers more than session logs

**Obscure passwords and secrets**, under Session Logging, applies to captured
configurations as well. One switch, one promise — two switches for the same
guarantee is a guarantee nobody can rely on.

### Window size is remembered by having one

**Remember window size and position** is expressed as "is a size stored",
because that is what it means. Unticking it clears what was stored, so the
next launch opens at the default size rather than silently restoring a stale
one.

## Resetting

There is no global reset. Each area has its own, because they fail
independently:

- **Platform Definitions → Reset all to defaults**, or delete `platforms.json`.
- **Output Colours → Reset to defaults**.
- The command library: `POST /api/snippets/reset`, or delete `snippets.json`.
- **AI Prompts → Reset**, or delete `prompts.json`.
- Everything else: delete `settings.json`. ShellMate starts as if new.

Deleting the whole `ShellMate-Data` folder resets everything, including your
saved connections and session history.
