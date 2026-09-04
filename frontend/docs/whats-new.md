# What's new

One section per release, newest first. The first run of a new version
offers this page; **Updates** in the sidebar asks GitHub whether a newer one
exists.

## 1.1.3

- **Output watch.** A colour rule under Settings → Output Colours can now be
  marked **Alert**, with its own severity and cooldown. The pattern is matched
  on the server, against every open session, so a line that matters on a tab
  you are not looking at reaches you anyway — with the window hidden, a
  critical one raises a notification from the tray.

- An **update channel** under Stockton → Diagnostics: `stable` offers only
  full releases, `beta` also offers prereleases for people trying a build
  early, marked Beta in the update window. **What's new in this version**
  under Diagnostics reopens these notes on demand.

- A licence key now tells the licence service where it is installed: the
  machine name, your user name, the operating system and the ShellMate
  version, when the key is entered, removed, and at each refresh. The
  holder of an organisation licence can see which seats are in use and
  which copies are behind. Nothing about your devices goes with it; the
  Licence section under Settings says exactly what is sent.

## 1.1.2

- An installed licence key now refreshes itself: once shortly after
  start and every few days after that, so a renewal made in the portal
  reaches your copy without pressing Refresh, and a revocation is learned
  within days. Nothing happens without a key, and no internet is not an
  error.
- A second small release, so that a copy on 1.1.1 has an update to apply.

## 1.1.1

- A small release published so that a 1.1.0 copy has something to update
  to — the first update applied from inside ShellMate. If you are reading
  this in the what's-new window after a restart, the swap worked.

## 1.1.0

- **Updates from inside ShellMate.** A newer release opens ShellMate's own
  window with the notes, downloads with a progress bar, is verified against
  the release's checksum, and swaps itself in on restart — with the previous
  copy put back if the new one does not start. The check runs at startup by
  default and stays harmless offline.
- **Licences.** A key under Settings → Licence, verified on this machine,
  is what lets ShellMate update itself; everything else works without one.
  Renewals arrive on refresh; revocations are learned the same way.
- The first launch of a new version opens a what's-new window instead of a
  toast.

## 1.0.1

The first release published on GitHub, so the update check has something to
find. Small fixes from the first day of 1.0:

- Every Claude model was being reported as retired; the cause was the
  Claude 5 family refusing the `temperature` parameter, and the fix sends it
  only to models that accept it.
- Enter in the Apply configuration editor adds a line; the checkbox sits
  beside its label; long context menus scroll instead of being cut off.
- Whole-tab group colour as an option, a hover card on every tab, search
  that highlights its matches, per-kind toast switches, and Check for
  updates in the sidebar and tray — with the answer shown even over the
  home screen.

## 1.0.0

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
