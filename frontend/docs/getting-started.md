# Getting started

ShellMate Portable is a terminal for network engineers with an AI assistant
attached. It runs from a single executable — no installer, no administrator
rights, no internet connection required.

## Running it

Put `ShellMate-Portable.exe` wherever you like and double-click it. A USB
stick is fine. So is a network share, a desktop folder, or a directory on a
locked-down build where you have no rights to install anything.

On first run it creates a `ShellMate-Data` folder **next to the executable**
holding everything it needs. Move that folder and your setup moves with it.

If the folder it runs from happens to be read-only — Program Files, a
write-protected stick — it falls back to your user profile and says so in the
log rather than failing.

## The window

It opens as a normal desktop application. Closing the window does **not** end
your sessions: ShellMate keeps running in the system tray with every
connection alive, which matters when you have shut the window on a device
that is halfway through a reload. Reopen it from the tray icon, or choose
**Quit** there to stop properly.

The interface is also a local web page, so you can point a browser at
`http://localhost:8765` whenever you want a second view or the browser's
developer tools.

## The home screen

What you see before any tab is open — and can always get back to by clicking
**ShellMate Portable** in the tab bar, or the logo in the sidebar. Either one
is Home: it clears whatever group was selected and brings the hero view
forward, even over a running terminal.

Down the side of the terminal area sits the **group tree** — your saved
connections arranged into named groups, once you have made some. Click a
group and its dashboard of connection tiles comes forward; click Home and you
are back to the unfiltered view. See [Connecting](#connecting) for what groups
can do.

The home view itself shows your most recent connections as tiles, doors into
the rest of the application — the manual, session history, SSH keys,
support — and a row of shortcut pills so the keyboard shortcuts are learnable
without opening a page like this one.

## Your first connection

Click **New Connection**, or press `Ctrl+T`.

| Field | Notes |
|---|---|
| Connection type | SSH, serial console, or telnet |
| Hostname / IP | The device address, or a COM port for serial |
| Port | Defaults to 22 for SSH and 23 for telnet |
| Username | Not required for telnet — many devices prompt in-band |
| Password | Optional if you are using a key, or have one saved |

The connection is saved automatically once it succeeds, so it appears on the
dashboard next time. Passwords are only saved if you ask.

## What happens when you connect

Several things, in the first second or two:

- The device is **identified** from its login banner — vendor, OS and version
  appear in the status bar.
- **Paging is turned off** using whichever command that platform needs, so
  `show run` does not stop every 24 lines. This is visible in the terminal;
  nothing is typed into your session without telling you. It happens only when
  the device was identified *confidently* — see
  [Device awareness](#device-awareness) for why that gate exists and what to do
  when it stops you.
- The **running configuration is captured** and compared with your last
  visit, so you are told if anything has changed since.
- The session starts **recording**, so you can search it later.

A note appears briefly in the bottom-right corner saying what was done and, if
nothing was, why not. None of this needs configuring, and all of it can be
switched off.

The AI panel is **not** shown until you ask for it — the robot icon in the
tab bar, beside the layout button, turns it on.

## Picking up where you left off

**Settings → ShellMate Interface → Reopen tabs when ShellMate starts**
reconnects the tabs you had open last time. It only restores sessions saved
as a connection with a stored password — nothing else can be reconnected
without asking you — and anything it could not restore is named rather than
silently missing, so "Restored 9 of 12" always comes with the list of three.

## The status bar

The line across the bottom describes the **active** session:
`SSH: core-sw-01 | Connected 01:05:12` — the state and, beside it, how long
the session has been up, counted from the moment the device answered.
Hovering any tab gives the same for that session, which is how you find the
one that has been up longest without crowding the tab strip.

A session that drops stops counting and says what it reached — *disconnected
after 02:11:04*. A clock still running on a dead session would be telling you
something untrue. Reconnecting starts a fresh one rather than resuming.

Next along is the **Device** chip — what ShellMate identified the device as,
and whether it was sure. Click it to say what the device is yourself; that
works even from the dashboard, where it still describes the last session
shown. It clears when no sessions remain, rather than describing a device
that is no longer connected to anything.

Beside the **Buffer** count is a small **copy** icon: it puts the most recent
output of the active terminal on the clipboard — two hundred lines by
default, set under Stockton → Terminal Behaviour. The tab menu's *Copy all
scrollback* is there for everything.

**Context** is the assistant's meter. It is an estimate until the provider
has answered once, and the provider's own count of the last request after
that; the tooltip shows tokens in and out for the last reply and for the
whole conversation. The [AI assistant](#assistant) page explains it.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+T` | New connection |
| `Ctrl+W` | Close the current tab |
| `Ctrl+1` … `Ctrl+9` | Switch to that tab |
| `Ctrl+F` | Find in the terminal |
| `Ctrl+Alt+1` … `Ctrl+Alt+9` | Choose a split layout (the rest are in the picker) |
| `Ctrl+Shift+B` | Broadcast a command to several devices |
| `Ctrl+Shift+C` | Copy the selection |
| `Ctrl+Shift+V` | Paste |
| `Ctrl+C` | Copy if text is selected, otherwise send an interrupt |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Next / previous tab (`Ctrl+PageDown` / `Ctrl+PageUp` in a browser) |
| `Ctrl+P` | Find a tab by name, hostname or group |
| `` Ctrl+` `` | Focus the terminal |
| `Ctrl+Shift+A` | Focus the assistant's input |
| `Ctrl+,` | Settings |
| `Ctrl+H` | Session history |
| `F1` | This manual |
| `Ctrl+/` | The list of shortcuts |

**Shift+right-click** in a terminal opens its menu — copy, paste, select all,
find, copy the screen or the scrollback, clear. A plain right-click pastes,
unless *Right-click Paste* is off in Settings, in which case it opens the menu.
The **copy** icon in the status bar copies the most recent output of the
active terminal; how many lines is in Stockton under Terminal Behaviour.

A tab can be given a name of your own from its right-click menu: **Rename
tab**. The name survives the device announcing its hostname, and clearing it
brings the detected name back.

Selecting text with the mouse — dragging or double-clicking — copies it.

### The tab strip by keyboard

Tabs are a tablist: Tab reaches the active one, the arrow keys move along
the strip and switch as they go, Enter or Space selects, and Delete closes.
Every right-click menu takes the arrow keys, Home and End, Enter and Escape,
and puts focus back where it was when it closes. `Ctrl+P` finds a tab by
name when the strip has overflowed. `Ctrl+/` lists every shortcut.

### Renaming a tab

Two consoles on the same stack print the same prompt. **Rename tab** in the
tab menu gives one a name of your own, which survives the device announcing
its hostname; clear the name to get the detected one back.

## Seeing several devices at once

The button beside **New** splits the terminal area into panes. It draws the
current layout rather than wearing an icon, so what is on screen is legible at
a glance.

| Layout | Panes |
|---|---|
| Single | 1 |
| Side by side | 2, left and right |
| Stacked | 2, one above the other |
| Three columns | 3 |
| Main and two | 3 — one large, two smaller beside it |
| Two over one | 3 — two above, one wide below |
| One over two | 3 — one wide above, two below |
| Quad | 4 |
| Three rows | 3, full-width rows |
| Two and main | 3 — two smaller, one large beside them |
| Six (3×2) | 6 |
| Eight (4×2) | 8 |
| Nine (3×3) | 9 |
| Twelve (4×3) | 12 |
| Sixteen (4×4) | 16 |

The first nine have `Ctrl+Alt` shortcuts; the grids are picked from the menu.
The big grids are for watching a wall of devices rather than working in them —
every pane is live, but sixteen tiny terminals is a dashboard, not a desk.

Open tabs fill the panes automatically. Everything still lives in the tab
strip; the panes decide which of those tabs are on screen together, and the
tabs that are showing are marked.

One pane has the keyboard, outlined in the accent colour, and that is the
session the status bar describes and the assistant reads. Click into another
pane to move the focus there.

To choose which device goes where, right-click its tab and pick a pane. If
something is already there the two swap places.

Switching to a layout with fewer panes hides the extra sessions; it does not
close them. They keep running, keep their scrollback, and come back the moment
you make room.

Each terminal is re-measured when its pane changes size and the device is told
the new dimensions, so output stays wrapped to the width you can actually see.

## Where things are kept

Everything lives in `ShellMate-Data` beside the executable.

| File | Contents |
|---|---|
| `settings.json` | Your preferences |
| `profiles.json` | Saved connections. Never contains a secret |
| `groups.json` | The groups and subgroups the dashboard tree shows |
| `vault.json` | API keys and saved passwords, encrypted |
| `credential-sets.json` | Named shared logins — the names and usernames only; passwords go to the vault |
| `credentials-plaintext.json` | Passwords you explicitly chose to keep readable |
| `shellmate.db` | Session history and configuration snapshots |
| `platforms.json` | What ShellMate knows about each device type |
| `schemes.json` | Terminal colour schemes, including any you add |
| `snippets.json` | The saved command library used by Broadcast |
| `prompts.json` | The assistant's system prompts, editable |
| `models.json` | The AI model lists discovered from each provider, cached |
| `shellmate.lock` | Stops a second copy starting against the same data folder |
| `logs/` | Session logs, if you turn them on |
| `configs/` | Captured device configurations, if you ask for them as files |
| `support/` | Diagnostic bundles you have built for a support request |
| `keys/` | SSH keys ShellMate made or imported |
| `window-storage/` | The desktop window's own state — scroll positions and the like |
| `shellmate.log` | What the application itself did, for troubleshooting |

`platforms.json`, `schemes.json` and `snippets.json` are plain, commented
JSON meant to be edited, kept in version control, or handed to a colleague.
Delete any of them and the shipped defaults come back.

`configs/` can be pointed anywhere, including a network share — see
[History and drift](#history-and-drift).

## Finding text in the terminal

**Ctrl+F** searches the buffer in front of you — the whole scrollback, not
just what is painted. Enter for the next match, Shift+Enter for the previous,
Escape to close and go back to typing.

This is a different question from **History**. History searches completed
commands and their output out of the database — *what did I change on the
Glasgow core last Tuesday*. It cannot reach a `show running-config` still
scrolling past, because that record is only written once ShellMate sees the
next prompt, and it finds nothing at all if recording is switched off.
Ctrl+F is for the four thousand lines already on your screen.
