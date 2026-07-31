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
welcome screen next time. Passwords are only saved if you ask.

## What happens when you connect

Several things, in the first second or two:

- The device is **identified** from its login banner — vendor, OS and version
  appear in the status bar.
- **Paging is turned off** using whichever command that platform needs, so
  `show run` does not stop every 24 lines. This is visible in the terminal;
  nothing is typed into your session without telling you.
- The **running configuration is snapshotted** and compared with your last
  visit, so you are told if anything has changed since.
- The session starts **recording**, so you can search it later.

None of this needs configuring, and all of it can be switched off.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+T` | New connection |
| `Ctrl+W` | Close the current tab |
| `Ctrl+1` … `Ctrl+9` | Switch to that tab |
| `Ctrl+Alt+1` … `Ctrl+Alt+7` | Choose a split layout |
| `Ctrl+Shift+B` | Broadcast a command to several devices |
| `Ctrl+Shift+C` | Copy the selection |
| `Ctrl+Shift+V` | Paste |
| `Ctrl+C` | Copy if text is selected, otherwise send an interrupt |

Selecting text with the mouse — dragging or double-clicking — copies it.

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
| Quad | 4 |

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
| `vault.json` | API keys and saved passwords, encrypted |
| `shellmate.db` | Session history and configuration snapshots |
| `platforms.json` | What ShellMate knows about each device type |
| `snippets.json` | The saved command library used by Broadcast |
| `logs/` | Session logs, if you turn them on |
| `shellmate.log` | What the application itself did, for troubleshooting |
