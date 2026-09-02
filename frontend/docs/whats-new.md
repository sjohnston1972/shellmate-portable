# What's new in 1.0

The first numbered release. Everything below arrived since the last build
that had no number, and each item links to the page that explains it.

## The assistant

- **It knows the device.** Every question carries the platform and version,
  a pending reload or commit-confirm, and when the configuration was last
  captured — read from what ShellMate had already established, never fetched.
- **It remembers the conversation.** The last few turns travel with each
  request; "and the other interface?" means what you think. On Claude the
  earlier turns are cached, so a long conversation does not cost its length
  again on every question.
- **Investigate mode.** A hypothesis, a plan of read-only steps as a
  checklist, one approved command at a time, and a conclusion.
- **Show output as rows.** Where ntc-templates has a template, the output of
  recent commands reaches the model parsed, beside the raw text.
- **Real token counts.** The meter uses the provider's own numbers once it
  has them, with cache hits shown.
- Ollama honours the same settings as the cloud providers, plus a context
  window and a keep-alive of its own.

See [AI assistant](#assistant).

## Connecting

- **Two-factor logins.** A device that asks for a code gets to ask you.
- **Port forwards** through the open session: local, dynamic SOCKS5, remote.
- **Apply configuration** with a preview of what would change, the guardrail
  in front of it, a diff afterwards, and a proposed way back.
- **Files**: rename, permissions, new folder, folder upload, folder download
  as a zip, recursive delete — all bounded.
- **IPv6** subnets and ranges in Find Devices.

See [Connecting](#connecting) and [SSH keys](#ssh-keys), a new page.

## History

- **Scheduled backups** per group: hourly, daily or weekly, through open
  sessions where they exist and short headless logins otherwise.
- **Timed replay** of any recorded session, at 1× to instant.

See [History and drift](#history-and-drift).

## The interface

- **Rename a tab**; the name survives hostname detection.
- **Find a tab** by name, hostname or group with `Ctrl+P`.
- A **terminal context menu** on Shift+right-click (or right-click, with
  right-click paste off): copy, paste, select all, find, copy the screen or
  the scrollback, clear.
- **Find** shows *3 of 17*.
- A **Copy output** button in the status bar.
- The rest of the **keyboard**: next and previous tab, Settings, History,
  the manual, focus the terminal or the assistant, and `Ctrl+/` for the list.
- One context menu for every menu: arrow keys, Home and End, Escape that
  works after any keystroke, focus put back where it was.
- Tabs are a keyboard-operable tablist; toasts are announced; a single focus
  ring; a forced-colours mode for Windows High Contrast.
- Clipboard failures say so instead of looking like a paste the device
  ignored.

See [Getting started](#getting-started).

## Under the hood

- A **version** and a build record — release, commit, build time — in the
  window title, Diagnostics, the support bundle and the log. **Check for
  updates** under Diagnostics, and an optional check at startup.
- The local API refuses requests whose Host header is not this machine, and
  cross-site writes, closing the two holes a page in the same browser could
  have used.
- Continuous integration on every push: every test, a build of the
  executable, signing when a certificate is present, and a release on a tag.
- Dependencies pinned for reproducible builds.

See [Settings](#settings) and [Broadcast and the API](#automation).
