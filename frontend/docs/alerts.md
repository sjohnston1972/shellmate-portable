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

A `commit confirmed` with no number is assumed to mean ten minutes, which is
the Junos default — settable, if your estate differs.

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
escalate as the deadline approaches — at ten minutes, five minutes, one minute
and ten seconds, unless you change the thresholds.

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

## Tuning it

Those switches decide *whether* each channel speaks. The numbers behind them —
when, how often, how loudly — are settable too, in the **Alert timing and
thresholds** subsection at the bottom of Settings → Alerts. They apply as soon
as you change them.

| Setting | Default |
|---|---|
| Escalation thresholds | 600, 300, 60 and 10 seconds remaining |
| Flash the tab within | The last 300 seconds |
| Toasts on screen at once | 3 |
| How long a toast stays | 12 seconds — the final warning stays until dismissed |
| Tone volume | 0.2 |
| Junos commit-confirm default | 10 minutes |
| Output watch cooldown | 60 seconds |
| Forget a pending action after | 6 hours |

Somebody who wants a nudge half an hour out adds 1800 to the thresholds. A
malformed list falls back to the default rather than leaving a pending reload
unannounced.

Which corner the toasts appear in is a layout question rather than an alert
one, so it lives with the layout settings: **Settings → ShellMate Interface →
Notifications appear**.

## Watching the output for a line

The two above are things ShellMate knows to look for. **Output watch** is the
one you define: any of your own colour rules under **Settings → Output
Colours** can be marked **Alert**, and from then on ShellMate tells you when it
matches — on whichever tab it happens.

That is the difference between it and the colour. A colour rule paints the
line on the way to the screen, so a `%LINK-3-UPDOWN` on a tab you are not
looking at is coloured on a screen nobody is watching. A watch rule is matched
in the background, on every open session at once, which is what `terminal
monitor` on four devices during a change actually needs.

Tick **Alert** on a rule and two more controls appear:

| Control | What it decides |
|---|---|
| Severity | `info`, `warning` or `critical`. Critical also sounds a tone, stays until dismissed, and — on the desktop build — raises a notification even with the ShellMate window hidden |
| Cooldown | Seconds before that rule may alert again. 60 by default |

The cooldown is not optional decoration. A flapping interface matches every
few seconds, and a `debug` prints the same keyword hundreds of times a second;
without a cooldown the rule alerts at exactly that rate, and an alert channel
that can do that is one people switch off for good. The default for a new rule
comes from **Output watch cooldown** in the alert timing settings; each rule
keeps its own number once written.

Two things worth knowing:

- **Matching happens on the server, against cleaned output.** Devices colour
  their own log lines, which buries escape codes inside the word — a pattern
  matched against the raw stream would miss them. ShellMate undoes that first,
  and only matches whole lines, so a keyword split across two chunks of output
  still counts.
- **"Enable highlighting" governs both.** Turning the colours off turns the
  watches off with them. One switch for one feature; two would mean somebody
  who quietened a session was still being interrupted by it.

Clicking the alert switches to the tab it came from. The rule that produced it
is in the tooltip.

## Stale alerts

A pending action nothing has been heard about for six hours — settable, above —
is dropped. Something was missed: the device rebooted and the socket survived,
or the cancellation scrolled past. A countdown stuck at zero for the rest of
the afternoon is worse than no countdown at all.

## Closing a tab with something pending

Closing a tab disconnects the session, which does not cancel anything already
scheduled on the device. ShellMate asks before closing a live tab
(**Settings → Interface**), but the confirmation is about the connection, not
about the reload — cancel it on the device first if that is what you meant.
