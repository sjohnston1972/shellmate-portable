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

The runner's API can list and run playbooks but has no endpoint that
accepts one, and its project directory is a bind mount from the container's
host. So **Send to the runner** copies the file to that path on the host,
over an SSH session ShellMate already has to it: pick the session, and it
says where the file landed.

If ShellMate has no session to the runner's host, the alternative is the
ordinary one — put the file in the folder yourself, or keep the project
directory in git and pull it there.

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

**Blocks** come first. Pick tasks from a short list — gather facts, run
show commands, push configuration lines, back up, save — fill in the
fields, and ShellMate assembles the YAML with the right modules for the
platform you chose. Nothing leaves the machine, nothing needs an API key,
and the same input gives the same playbook every time.

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

Leave the box empty for `requirements.yml`, or name another file inside the
runner's project directory — a repository that keeps its own is the usual
reason. A file that is not there is reported as missing rather than
quietly falling back to the default, so naming one that turns out to be
wrong fails where you can see it.

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
