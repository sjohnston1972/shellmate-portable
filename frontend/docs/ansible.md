# Ansible

ShellMate does not run Ansible. It drives a container that does, and shows
you what is happening: which playbooks exist, what a run is doing task by
task, what it changed, and what it said when it failed.

The container wraps `ansible-runner` in a REST API. You run it wherever
Docker lives — a jump host, a VM in the management network, a laptop.
ShellMate talks to it and nothing else.


## Why a container rather than Ansible here

Ansible is a Python application with a large dependency tree and a strong
opinion about running on Linux. ShellMate is a single portable executable
that has to start on a locked-down Windows laptop with no install. Those
two things do not fit in one file, and pretending otherwise would mean a
150 MB download that still could not run a playbook needing a collection
you have not installed.

Keeping Ansible where it already lives has a second benefit: the runner is
on the management network, so the playbook reaches devices ShellMate might
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

The runner requires a bearer token on every API call, and refuses to start
bound to anything but loopback without one. The token lives in a `.env`
file on the container's host. Put the same value in ShellMate under
**Settings → Ansible → Token**; it goes into the encrypted vault, never
into `settings.json`.

`/health` is deliberately open, so ShellMate can tell three states apart:
not set up, cannot be reached, and reachable but refusing us. The third
says so and points at the token rather than sending you to a firewall.

> **The token crosses the network in the clear.** The runner speaks plain
> HTTP. On a trusted management LAN that is a reasonable trade; the day the
> container moves anywhere else, put TLS in front of it and give ShellMate
> the CA certificate. ShellMate supports HTTPS and, if the deployment adds
> it, a client certificate as well.

### In ShellMate

Open **Settings → Ansible** and fill in:

| Field | What it is |
|---|---|
| Runner URL | `http://runner.example:8081`, or `https://…` behind TLS |
| Token | The bearer token from the runner's `.env` |
| Client certificate, key, CA | Only if a deployment puts mutual TLS in front |
| Verify TLS | Leave on with a real certificate; off only for a self-signed one |
| Project directory | The **host** path the container mounts as `/runner/project` |

**Test connection** asks the runner what it holds and says what came back,
or exactly what failed. A certificate path that is not a file is named as a
missing file rather than reported as a connection problem.

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
panel. YAML is checked when you save, so a mistake is caught here with the
parser's own message rather than three steps later when a run refuses to
start.

### Getting yours to the runner

The runner's API can list and run playbooks but has no endpoint that
accepts one, and its project directory is a bind mount from the container's
host. So **Send to the runner** copies the file to that path on the host,
over an SSH session ShellMate already has to it: pick the session, and the
panel says where the file landed.

If ShellMate has no session to the runner's host, the alternative is the
ordinary one — put the file in the folder yourself, or keep the project
directory in git and pull it there.

## Running one

**Run** asks four things:

- **Which playbook**, from either set.
- **What to run it against.** Either a group from your own estate, or
  specific hosts, or an inventory the runner already holds. The estate
  option turns your saved connections into hosts and your groups into
  Ansible groups.
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

## The estate as an inventory

Your saved connections and groups can be the inventory, generated fresh
for each run:

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

Nothing is pushed anywhere. The inventory is generated when you start the
run and **travels with it**: the runner writes it to a file for that job
alone and never touches the inventory it holds. So there is no second copy
of your estate to keep in step, and a run is pointed at exactly what you
chose rather than at whatever was pushed months ago.

**Show what would be sent** displays it before you start — the hosts, and
the ones left out with their reason. While a run is going, and afterwards
in its history, the panel says which inventory it was actually pointed at:
the one ShellMate sent, or a path the runner holds. In a change record
those are different claims, and after the fact they are otherwise
indistinguishable.

If you would rather the runner held a copy permanently, put a file in its
`/runner/inventory` directory yourself; anything there merges with the
rest automatically.

## Reporting

A finished run can be copied as a report: what ran, against what, which
hosts changed, which failed and why, ready to paste into a change record or
a ticket. The panel also keeps a short list of the runs you started from
this machine — that is ShellMate's own note, so a run somebody started
elsewhere will not be in it. **Jobs** asks the runner itself, which knows
about every run including those from before it was last restarted.

## What can go wrong

**"No Ansible runner is set up yet."** Nothing is configured. Settings →
Ansible.

**"Could not reach the runner at …"** The address is wrong, the container
is not running, or the network in between is not letting you through. The
message names the address it tried.

**A certificate error.** Either the client pair is not the one the service
trusts, or its own certificate is not signed by the CA you gave. With the
development certificate, turn Verify TLS off; with a real one, check the CA.

**"playbook file not found".** The runner's own words. The playbook is not
in its project directory — likely one of yours that has not been sent
across yet.

**"The runner is there but will not accept ShellMate."** The token is
missing or wrong. It is the value from the runner's `.env`, and it goes
under Settings → Ansible.

**A run against the estate says nothing would run.** Every connection in
that group is serial, or has no address. Ansible has nothing to dial.
