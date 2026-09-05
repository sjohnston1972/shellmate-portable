# Ansible

ShellMate does not run Ansible. It drives a container that does, and gives
you somewhere to work: what you hold, what a run is doing task by task,
what it changed, and what it said when it failed.

Ansible has its own view rather than a side panel, because it is somewhere
you stay for a while. Open it from the sidebar. The terminals carry on
behind it — a device mid-reload is still there when you come back — and
leaving puts you back on exactly what you were looking at.

## The eight areas

| Area | What it is for |
|---|---|
| **Dashboard** | What is running, what you hold, and anything wrong with the runner |
| **Playbooks** | The runner's own and yours; writing one, sending it, running it, watching it |
| **Builder** | Getting to a first playbook without writing YAML |
| **Templates** | A parameterised play, filled in each time |
| **Inventory** | What a run would actually be pointed at, before it runs |
| **Environments** | Named settings a run inherits |
| **Keys** | Credentials a run needs, held in the vault |
| **Repositories** | Where a set of playbooks came from |

## Why a container rather than Ansible here

Ansible is a Python application with a large dependency tree and a strong
opinion about running on Linux. ShellMate is a single portable executable
that has to start on a locked-down Windows laptop with no install. Those
two things do not fit in one file, and pretending otherwise would mean a
150 MB download that still could not run a playbook needing a collection
you have not installed.

Keeping Ansible where it already lives has a second benefit: the runner is
on the management network, so a playbook reaches devices ShellMate might
only see through a jump host.

## Setting it up

### The container

The runner is a small service wrapping `ansible-runner`, built on a current
Python base with Ansible and the AWS, Azure and Meraki collections
installed. It publishes an API and mounts two directories from its host:

| Inside the container | What it holds |
|---|---|
| `/runner/project` | the playbooks it can run |
| `/runner/inventory` | its own inventory, with `group_vars` and `host_vars` |

Both are bind mounts, which matters later: a playbook written in ShellMate
is copied to the **host** path that the container mounts, not to a path
inside the container.

### The token

The runner requires a bearer token on every API call. The token lives in a
`.env` file on the container's host. Put the same value in ShellMate under
**Settings → Ansible → Token**; it goes into the encrypted vault, never
into `settings.json`.

`/health` is deliberately open, so ShellMate can tell three states apart:
not set up, cannot be reached, and reachable but refusing us. The third
says so and points at the token rather than sending you to a firewall.

### TLS

The runner speaks HTTPS. Its certificate is self-signed, so the same file
is both the certificate and the thing that vouches for it: copy
`runner/tls/api.crt` from the container's host and point **CA certificate**
at your copy.

Check the copy against the fingerprint the container prints at startup
rather than trusting the file arrived intact:

```
docker logs ansible-runner 2>&1 | grep '^TLS:'
```

The certificate is generated once and reused on every start, so a restart
or a rebuild does not change it. It changes only if the file is deleted, if
it expires, or if the list of names it covers is extended.

That list matters. The certificate covers `localhost`, `127.0.0.1`, `::1`,
the container name, the host name, and the addresses the container answers
on. **If you reach the runner on an address not in that list, verification
fails even though the connection succeeded.** The fix is to have the
address added, not to turn verification off.

> **Verify TLS off is a real choice with a real cost.** It stays available
> because a development certificate is a legitimate reason to want it. When
> it is off, the header says **Connected, unverified** and the dashboard
> says why — the connection is encrypted, but nothing is checking who is on
> the other end.

### The security light

The Ansible view's header carries a light beside the runner pill, and they
answer different questions on purpose. The pill says whether the runner can
be used. The light says whether anything is checking who is on the other
end — because those fail independently, and the case worth catching is the
one where the pill is green.

| Light | What it means |
|---|---|
| Grey — Not set up | No runner address yet. Nothing has failed |
| Red — Unreachable | Nothing answered |
| Red — Certificate expired | It stopped being valid; the date is in the message |
| Red — Certificate not trusted | Something answered and ShellMate would not trust it |
| Amber — Not encrypted | Plain HTTP. It works, and everything crosses in the clear |
| Amber — Not verified | Encrypted, but Verify TLS is off. Nothing is checking |
| Amber — Certificate expiring | Working today, and inside 30 days of stopping |
| Amber — Token refused | The connection is sound; the token is not |
| Green — Secure | Encrypted, verified, and the runner accepted us |

When two things are wrong at once the worse one wins. An expired
certificate causes a refused token as often as not, and reporting the token
would send you hunting through a `.env` file for what is actually a date.

**Click the light** for the certificate ShellMate is talking to: who it was
issued to, when it expires, which names and addresses it covers, the TLS
version, and its SHA-256 fingerprint.

Compare that fingerprint against the one the container prints at startup.
That comparison is the only thing that makes a self-signed certificate
trustworthy, and it only works because the two values reach you by
different routes — one over the connection in question, one from the
container's own logs. ShellMate deliberately cannot do it for you: taking
the certificate off the connection and then treating it as the thing that
vouches for that connection is a circle, and it would trust an interceptor
just as readily as the real container. So ShellMate shows what it sees, and
stops there.

The runner will not hand its certificate out over its API either, for the
same reason and by the same deliberate choice. Both halves are needed:
either one alone would close the circle.

The check runs every 30 seconds while the view is open and every 5 minutes
when it is not, and stops entirely while the window is hidden.

Mutual TLS is supported and not required. If a deployment puts a client
certificate in front of the runner, give ShellMate the pair; otherwise
leave those fields empty.

### In ShellMate

Open **Settings → Ansible** and fill in:

| Field | What it is |
|---|---|
| Runner URL | `https://127.0.0.1:8081`, or wherever the container is |
| Token | The bearer token from the runner's `.env` |
| CA certificate | Your copy of the runner's certificate |
| Verify TLS | Leave on. See above |
| Client certificate, key | Only if a deployment puts mutual TLS in front |
| Project directory | The **host** path the container mounts as `/runner/project` |

**Test connection** asks the runner what it holds and says what came back,
or exactly what failed. A certificate that will not verify is reported as a
certificate problem, not as a network one — they have different fixes, and
the switch people reach for when told "cannot reach" is the wrong one.

## How devices are reached

The runner logs in to your devices, not ShellMate, so the credentials it
uses are its own. Deploy an SSH key to the container and to your devices;
ShellMate never sends a device password to the runner.

That is a deliberate boundary. A password travelling to a container so it
can be handed to Ansible is a password in two more places than it needs to
be, and the vault exists so there are not two more places.

## Playbooks

Two sets, shown together and clearly distinguished.

**The runner's own** are whatever is in its project directory, including
those in subdirectories. They are listed with their size, can be opened
read-only, and can be run. This is what a team keeping playbooks in git
will use.

**Yours** are a library kept with your ShellMate data, editable in the
view. YAML is checked when you save, so a mistake is caught here with the
parser's own message rather than three steps later when a run refuses to
start.

### Getting yours to the runner

**Send to the runner** puts the file on the runner over its API. It reports
where it landed and how many plays it parsed — which is worth reading, since
"2 plays" means the file arrived as a playbook rather than as text that
happens to be sitting there.

A name already on the runner is not replaced silently. You are asked, and
replacing cannot be undone from here.

Older containers had no endpoint that accepted a playbook. Against one of
those, ShellMate falls back to copying the file over an SSH session you
already have to the machine hosting the container, into the path it mounts
as its project directory — so that route needs a session to that host and
says so. If you have neither, the ordinary alternatives remain: put the file
in the folder yourself, or keep the project directory in git and pull it
there.

## Running one

**Run** asks four things:

- **Which playbook**, from either set.
- **What to run it against.** Either a group from your own estate, or
  specific hosts, or an inventory the runner already holds.
- **Variables and tags**, if the playbook takes them.
- **Check mode**, which is Ansible's dry run: it reports what it *would*
  change and changes nothing. It is the honest way to try a play against
  an estate you have not run it on before, and it is offered first for
  that reason.

The run then streams: each task as it starts, each host as it answers, and
a tally of ok, changed, failed, unreachable and skipped. Clicking a task
shows its own output, which is where a failure explains itself. **Stop**
asks the runner to cancel; a run that has already finished says so rather
than pretending it stopped something.

## Builder

Two ways to get to a playbook, and the order is the argument.

The canvas is the playbook, drawn as the thing it is: a playbook holding
plays, each play holding tasks, and tasks notifying handlers. Each level
carries its own **+ add**, so where you click is where the thing lands.

**Drag from the group tree on the left.** That tree is ShellMate's own
directory of the estate and it stays on screen while the Ansible view is
open, so there is no second list of the same sites to keep in step. Drag a
site, a subgroup or a single device onto the playbook to add a play for it,
or onto an existing play to change what that play targets. Dropping onto a
play that already targets something extends the pattern rather than
replacing it — Ansible reads a comma-separated list as a union.

A whole site works as a target even though nothing is tagged with the site
itself: the generated inventory declares it as a group of the groups
beneath it, which is Ansible's own way of saying so. A serial console
cannot be a target and says why, because it has no address to dial.

**Tasks** come from a short list — gather facts, run show commands, push
configuration lines, back up, save — and ShellMate assembles the YAML with
the right modules for the platform you chose. Nothing leaves the machine,
nothing needs an API key, and the same input gives the same playbook every
time.

**Or describe it.** With an AI provider configured, write what you want in
a sentence and get a draft. It uses whichever model the chat panel is set
to, so there is one place to choose and one answer to "which model wrote
this".

Whichever produced it, **what it would do** is read back underneath: every
task, its module, and whether it changes the device or only reads from it.

> A draft from a model is labelled a draft everywhere it appears, and is
> never saved without you choosing to save it. A model writes something
> plausible and wrong with exactly as much confidence as something right,
> and ShellMate cannot tell the difference either. The read-back exists to
> make checking it quick, which is the only defence there is.

A module ShellMate does not recognise is counted as changing the device and
named, so you can go and look. "Could not tell" and "safe" are different
claims.

## Templates

A template is a play with named holes in it — "shut an interface", "set NTP
servers" — and a description of each hole. Filling one in produces a
playbook, which is what makes the same change repeatable by somebody who
did not write it.

Writing one: put `{{ variable }}` in the body, and describe each variable
with a label, optional help, a default, and optionally the values it may
take. **Detected holes** finds the ones you have not described yet and adds
a row for each. A template whose body uses a hole that nothing describes is
refused — the form could not ask for it, and the run would fail on an
undefined variable somewhere in the middle of a play.

Substitution is literal, not Jinja. A template is text somebody typed, and
running it through a template engine inside ShellMate would turn a text box
into a way to execute code. Ansible still evaluates its own Jinja when the
play runs.

Whether a template changes a device is **read off its body**, not ticked in
a box. The badge would otherwise be one honest mistake away from making
something look safer than it is.

## Inventory

What a run would actually be pointed at, shown before anything runs.

Your saved connections and groups can be the inventory, generated fresh for
each run:

- A connection becomes a host, addressed the way ShellMate dials it.
- Its name travels as `shellmate_name`, so a report can say `core-1`
  rather than an address.
- Its group becomes an Ansible group, with the characters Ansible does not
  allow replaced: `site-004/core switches` becomes
  `site_004_core_switches`.
- The platform ShellMate identified sets `ansible_network_os` and the
  `network_cli` connection, so a switch is treated as a switch rather than
  as a server with a shell. A device ShellMate has not identified gets
  neither, which is the honest default.
- A serial connection is **left out**, and named, with the reason: there is
  no address for Ansible to reach.

The hosts left out are shown as prominently as the ones included. A run
that silently omits half a site is the thing this screen exists to prevent.

Nothing is pushed anywhere. The inventory is generated when you start the
run and **travels with it**: the runner writes it to a file for that job
alone and never touches the inventory it holds. So there is no second copy
of your estate to keep in step, and a run is pointed at exactly what you
chose rather than at whatever was pushed months ago.

If you would rather the runner held a copy permanently, put a file in its
`/runner/inventory` directory yourself; anything there merges with the rest
automatically. There is no way to list it from here — the runner has no
endpoint for that — so the choice is its own default or a path you name.

### Custom inventories

A run usually wants less than the whole estate, and sometimes wants
something ShellMate has never connected to at all. Both are built in this
area and appear as a target in the Run dialog.

**Picked out of the estate.** Tick hosts in the table and save them as a
named list. "The switches I am upgrading this weekend" is not a group and
should not have to become one — making it a group changes the tree
everybody else sees, for a list that stops mattering on Monday. The rows
carry what ShellMate already knew: the address, the name, the username,
the port and the platform.

**Uploaded from somewhere else.** A CSV or a plain list of addresses — a
Meraki export, an IPAM report, a spreadsheet somebody keeps. Two things
are asked rather than guessed:

- **Which column holds the address.** A heading may say `LAN IP`, `mgmt`,
  `ip_address` or nothing at all. Picking one by pattern produces a list
  that is well-formed, looks populated and dials nothing — and the run
  that follows reports a problem about hosts rather than about the file.
- **Whether the first row is a heading.** ShellMate concludes one and
  shows you which, as a tick box. Getting it wrong loses something either
  way: a heading read as a device adds a host called `ansible_host`, and a
  device read as a heading drops one switch out of forty and says so
  nowhere.

**Nothing invents a platform.** An uploaded row gets no
`ansible_network_os` unless you say what the devices are. A wrong one is
worse than none: it makes Ansible treat a firewall as a switch, and the
failure then arrives from a module several steps away from the cause.

Worked examples of each shape are shipped with ShellMate. *Try it* loads
one through the same parse a real upload goes through; the download icon
beside it saves the file, so your own export can be made to match.

## Playbooks in GitHub

The library holds the current version of a playbook and the runner holds a
copy of it. Neither is a history. A file that changes a hundred devices
should be able to answer what it looked like last Tuesday and who changed
it, so ShellMate can commit each save to a GitHub repository.

Set it up in **Settings → Ansible → Playbooks in GitHub**. Five things are
worth knowing, because each of them is a way this could go quietly wrong:

- **The token is yours, and it lives in the vault.** Like every other
  credential, it is encrypted and never written to `settings.json`.
  ShellMate deliberately does *not* read a `GITHUB_TOKEN` environment
  variable: that name already exists on a great many machines, and picking
  it up would commit your estate under somebody else's identity with
  nothing on screen saying so.
- **Two ways to point it at a repository.** *Create a private repository*
  needs a token that can create repositories. *Use the one named above*
  needs only a token that can push to one that exists — a much smaller
  permission, and the reason both are offered rather than one falling back
  to the other. The repository is read before it is stored, so a name that
  is wrong is said now rather than at the next save.
- **Private unless you say otherwise.** A playbook carries hostnames,
  addresses and the shape of your estate, and a repository made public by
  accident cannot be un-published. Anything ShellMate creates is private.
- **The playbook, and nothing else.** Never the inventory. That is
  generated from your estate, it is the whole device list, and a
  repository's visibility can be changed later by anyone who can reach it.
- **A commit that fails never costs the save.** The playbook is written to
  the library first. If GitHub cannot be reached, the editor says "saved
  here, but not committed" and names the reason — that order is the point
  of the sentence.

Playbooks are committed to a `playbooks/` directory in the repository,
which is also where `ansible-runner` expects to find them.

## Environments

An environment is a named set of run settings: which inventory, which
limit, the variables, how many forks, how much detail. The point is that
"run it against production" becomes one choice rather than six fields typed
the same way every time.

**Force check mode** is the one worth knowing about. An environment can
insist that every run against it is a dry run. That only ever works in one
direction — an environment can turn checking on, and nothing in a run
request can turn it back off. "Production, and I mean it" should take a
second decision rather than the same click as staging.

## Keys

A playbook run often needs a credential ShellMate does not otherwise hold:
an Azure client secret, a Meraki API key, an Ansible Vault password. They
cannot live in the playbook and they must not be typed in every time.

So they live in the encrypted vault under a name, and a run refers to them
**by that name**. Each key says how it is delivered:

- **As an environment variable** — visible to every task in the play. This
  is what a cloud collection usually expects.
- **As an extra var** — a variable the playbook has to name to use.

The value is resolved from the vault at the moment the run starts. A key
the vault cannot currently read stops the run and says which one, rather
than sending a blank credential and failing three tasks later somewhere
unrelated.

Three things are true and worth stating plainly:

1. **There is no way to read a value back.** Listing keys returns names,
   kinds and whether the vault can read them. Nothing returns a value; the
   only way one leaves ShellMate is with a run. A key you cannot remember
   has to be replaced.
2. **The value does reach the runner.** It has to — Ansible is what uses
   it. What the vault buys is that the secret is not in a playbook, not in
   a file on the container, and not in a shell history. It is not
   end-to-end secrecy.
3. **While the run executes, the value is in the container's process
   environment**, and a playbook that prints the variable leaks it itself.
   `no_log: true` is the play author's responsibility, and nothing
   ShellMate or the runner's API does can prevent it.

Editing a key without retyping the value keeps the stored one, so changing
where a key is delivered does not mean digging the secret out again.

## Repositories

The runner has no git of its own, and ShellMate does not clone anything: a
portable executable carrying a git implementation to drive a container it
cannot reach directly is the wrong shape. So this area is a **record**, not
a sync — the remote, the branch, the path, and the last revision you noted.
Enough to say "the runner is three commits behind" and enough to put in a
change record.

Files still get to the runner the ordinary way: put them in its project
directory on the container host, or keep that directory in git and pull it
there.

**Install collections** asks the runner to install what its playbooks need.
A repository usually brings its collections with it in a requirements file,
and "module not found" three tasks into a run is what happens when nobody
has run this.

Leave the box empty for `requirements.yml`, or name another file — a
repository that keeps its own is the usual reason. The runner resolves the
name against `/runner` and then `/runner/project`, so
`myrepo/requirements.yml` is enough; you do not have to write the project
directory in front of it.

A file that is not there is reported as missing rather than quietly falling
back to the default, so a name that turns out to be wrong fails where you
can see it. A path that climbs out of those directories, or starts at the
root, is refused outright.

## Reporting

A finished run can be copied as a report: what ran, against what, which
hosts changed, which failed and why, ready to paste into a change record or
a ticket. The view also keeps a short list of the runs you started from this
machine — that is ShellMate's own note, so a run somebody started elsewhere
will not be in it. **Jobs** asks the runner itself, which knows about every
run including those from before it was last restarted.

While a run is going, and afterwards in its history, the view says which
inventory it was actually pointed at: the one ShellMate sent, or a path the
runner holds. In a change record those are different claims, and after the
fact they are otherwise indistinguishable.

## What can go wrong

**"No Ansible runner is set up yet."** Nothing is configured. Settings →
Ansible.

**"Certificate refused."** Something answered and ShellMate would not trust
it. Either the CA file is not the runner's certificate, or you are
connecting on an address the certificate does not cover. Neither is a
network problem, and turning Verify TLS off is not the fix.

**"Could not reach the runner at …"** The address is wrong, the container
is not running, or the network in between is not letting you through. The
message names the address it tried.

**"The runner is there but will not accept ShellMate."** The token is
missing or wrong. It is the value from the runner's `.env`, and it goes
under Settings → Ansible.

**"playbook file not found".** The runner's own words. The playbook is not
in its project directory — likely one of yours that has not been sent
across yet.

**A module is not found.** The collection is not installed on the runner.
Repositories → Install collections.

**A run against the estate says nothing would run.** Every connection in
that group is serial, or has no address. Ansible has nothing to dial.

**"No readable value for …"** A run asked for a key the vault cannot
currently produce. Unlock the vault, or set that key's value again.
