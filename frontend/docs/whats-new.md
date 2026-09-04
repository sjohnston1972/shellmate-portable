# What's new

One section per release, newest first. The first run of a new version
offers this page; **Updates** in the sidebar asks GitHub whether a newer one
exists.

## 1.1.3

- **Five more platforms: IOS-XR, FortiOS, MikroTik RouterOS, Huawei VRP and
  Aruba AOS-CX** — with the prompt shapes to match, which is the half that
  matters. ShellMate reads a prompt to name the tab, cut the session into
  commands, expand aliases and hold destructive ones, and on a device whose
  prompt it did not recognise all of that quietly did nothing. It now knows
  `RP/0/RSP0/CPU0:edge-xr#`, `FGT-01 (global) #`, `<core-sw1>`, `[core-sw1]`
  and `[admin@MikroTik] >`. Paging is turned off with `terminal length 0` on
  XR, `screen-length 0 temporary` on VRP and `no page` on AOS-CX; FortiOS and
  RouterOS are sent nothing, because neither has a command that turns paging
  off for one session and nothing else.
- **Edit, connect or disconnect a selection of connections.** Ctrl+click
  several in the tree and the menu now offers **Edit N connections…** —
  username, port, connection type, platform, shared credential, jump host and
  authentication, each starting at "leave as they are" — along with
  **Connect N** and **Disconnect N**. Passwords are never edited in bulk, and
  an edit that would give two connections the same identity skips that one and
  says "would merge with sw1" rather than merging it and losing a credential.

- **Group defaults.** A group can now hold the username, shared credential,
  platform, port and jump host that everything in it uses — **Group
  defaults…** on its menu — and every connection whose own field is blank
  inherits them, subgroups included. Two hundred sites behind two hundred
  bastions is two hundred edits when one moves; this makes it one. A
  connection's own value always wins, nothing is written onto it, and where
  two groups disagree the field is inherited by nobody and the dialog names
  the two groups rather than guessing.

- **Import and export the estate as CSV.** Right-click the group tree's
  background for **Import connections…** — paste rows from a spreadsheet or
  choose a file — and **Export all as CSV…**; any group's own menu exports
  that group and everything nested under it. A preview says how many rows
  are new, how many are already saved, and names each one it could not read
  before anything is written. A file carrying a `password` column is refused
  outright rather than having the column quietly dropped.

- **Check reachability** on a group, a connection, or a selection of them.
  Every member is probed on its own port; the dot in the tree gains a third
  state, the hover text says `port 22 open, SSH-2.0-Cisco-1.25, 38 ms` or
  `port 22 refused`, and a summary says "47 of 50 reachable" and names the
  ones that did not answer. It never runs on a timer, and an open port is
  reported as an open port rather than as a healthy device.

- **The vault can travel.** Settings → Credentials Vault gains **Export
  backup…** and **Import backup…** — the whole vault under a passphrase you
  choose, restorable into whatever mode the machine at the other end uses.
  And when ShellMate finds a vault it cannot decrypt here — a stick carried
  to a second laptop, or a different Windows account — it now says so at
  startup and offers to import a backup or start a new vault, keeping the
  old file aside, rather than quietly behaving as though the vault were
  empty.

- **Output watch.** A colour rule under Settings → Output Colours can now be
  marked **Alert**, with its own severity and cooldown. The pattern is matched
  on the server, against every open session, so a line that matters on a tab
  you are not looking at reaches you anyway — with the window hidden, a
  critical one raises a notification from the tray.

- **Command recall.** `Ctrl+R` in a terminal lists every command already run
  on that device, newest first and deduplicated — Enter puts one at the
  prompt, Ctrl+Enter runs it. A history search result whose device is open
  gains the same two buttons, aimed at the tab matching its recorded
  hostname rather than at whichever tab is in front of you.

- **Quick connect.** `Ctrl+P` now takes an address as well as a tab name:
  `10.1.20.5`, `admin@host:2022`, `telnet host 2003` or `COM5 115200`. The
  first row shows exactly what will be dialled, a saved connection for the
  same address wins and opens with its own credentials, and an address
  already in your clipboard is filled in for you.
- **The assistant can see what changed.** The configuration diff from the
  connect-time drift check now travels with your questions, and an **Explain**
  button in the diff window asks what the changes do and whether they could
  cause what you are seeing — for the drift since your last visit, a
  configuration you have just applied, or any two captures you pick. `/diff`
  in the chat asks the same thing. The diff is masked and capped on the
  server, like everything else that leaves the machine.

- **Review with the assistant**, on a configuration preview. Before you press
  Apply, the classified lines and the stanzas they land in go to the
  assistant with a fixed review: what the change does, ordering problems, a
  missing `no shutdown` or `commit`, what else it touches, and the lines that
  would undo it. Nothing is sent to the device, and the preview stays open
  beside the answer.

- **Run checks** in Settings → Diagnostics. A row of chips saying whether
  this installation is healthy: which window frame took it, whether the data
  folder is the one beside the executable, whether the vault seals and reads
  back, FTS5, the port that was bound, a leftover copy from an update, and
  the feedback outbox — with what to do about anything amber or red. The two
  network checks are opt-in, per press. The same checks go into the support
  bundle as `checks.txt`.
- **Searching Settings highlights every hit**, not just the row: the label,
  the description, the tooltip and the section heading. The count beside the
  box says how many and where, Enter steps through them, and the rail keeps
  every section listed with a count rather than emptying as you type.
- **A saved connection remembers what the device is**: its release, model,
  serial number and when you last opened it, learned from the device itself
  as it connects and captures. Search the tree for a model number or a
  release to list everything carrying it, see them on the hover cards, and
  take them out in the CSV export.
- Session history can be searched **between two dates**, with optional
  times, instead of only the last day, week, month or quarter.
- Session playback shows where it has reached: a draggable bar with the
  command number and the elapsed time, a Pause button, Space to pause and
  the arrow keys to step. The speed can be changed while it plays.
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
