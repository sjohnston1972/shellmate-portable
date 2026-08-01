# Alerts

Some commands leave a clock running. `reload in 10` schedules a reboot in ten
minutes; a Junos `commit confirmed` rolls your change back unless you confirm
it. Both are consequential, both are easy to lose track of, and in an ordinary
terminal both are invisible the moment the output scrolls away.

ShellMate watches for them and puts a countdown where you can see it.

## What it watches for

| Kind | Trigger | What happens if nobody acts |
|---|---|---|
| Reload | `reload in 10`, `reload at 23:00`, `request system reboot in 5` | The device reboots |
| Commit confirm | `commit confirmed`, `commit confirmed 5` | The change is rolled back |

The patterns come from the platform definitions, so a device ShellMate has not
been taught about produces no alerts. See
[Device awareness](#device-awareness) for how to add one.

## Where the countdown appears

**On the tab** — a countdown beside the device name, so it is visible whichever
tab you are on.

**In the status bar** — for the session you are looking at, with the kind and
the time remaining.

Neither of these is switchable. They are the information, not the
interruption.

## How it knows the time

Two sources, and they answer different questions.

**What you typed.** Known the instant you press Enter, and still true after
the device's reply has scrolled off the top. But `reload in 10` states an
intention: the device rounds, and its clock is not yours.

**What the device said.** `*** --- SHUTDOWN in 0:09:58 ---` is authoritative,
and IOS repeats it as the deadline approaches, so the countdown
re-synchronises rather than drifting.

The typed command opens the alert; the device's own answer refines it. Where
only the first is available, ShellMate says a reload was *requested* and shows
no countdown at all — a confident wrong timer is worse than an honest vague
one.

Cancelling is watched for in the same way: `reload cancel`, `SHUTDOWN
ABORTED`, a plain `commit`. The alert clears.

## The channels

Three ways of being told, each switchable under **Settings → Alerts**. They
escalate at ten minutes, five minutes, one minute and ten seconds.

**Flash the tab.** The tab pulses over the last five minutes only. A tab that
pulses for ten minutes is one people stop seeing.

**Audible warning.** A short tone at each threshold, synthesised in the
browser. No sound file is fetched, so it works on a machine with no network at
all.

**Pop-up notifications.** A toast naming the device and the time left.
Clicking it switches to that tab. This is the channel that reaches you when
you are looking at something else, which is where most of these will find you.
The final warning stays until dismissed; the earlier ones fade.

**Reduce motion** replaces the flashing with a steady outline. Your system's
own reduced-motion setting is honoured as well; this forces it on regardless.

## Stale alerts

A pending action older than six hours is dropped. Something was missed — the
device rebooted and the socket survived, or the cancellation scrolled past —
and a countdown stuck at zero for the rest of the afternoon is worse than no
countdown at all.

## Closing a tab with something pending

Closing a tab disconnects the session, which does not cancel anything already
scheduled on the device. ShellMate asks before closing a live tab
(**Settings → Interface**), but the confirmation is about the connection, not
about the reload — cancel it on the device first if that is what you meant.
