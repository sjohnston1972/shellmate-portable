# Ansible

ShellMate does not run Ansible. It drives a container that does, and shows
you what is happening: which playbooks exist, what a run is doing task by
task, what it changed, and what it said when it failed.

The container is [ansible-runner-service][ars], which wraps `ansible-runner`
in a REST API. You run it wherever Docker lives — a jump host, a VM in the
management network, a laptop. ShellMate talks to it and nothing else.

[ars]: https://github.com/ansible/ansible-runner-service

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

Follow the runner service's own instructions. In short:

```bash
docker run -d --name ansible-runner-service -p 5001:5001 \
  -v /srv/ansible/project:/usr/share/ansible-runner-service/project \
  -v /srv/ansible/certs:/usr/share/ansible-runner-service/certs \
  ansible-runner-service
```

The volumes matter. The **project** directory is where playbooks live —
the API can list and run them but cannot accept one, so this is the folder
anything you write has to reach. The **certs** directory holds the
certificates, including the client pair you are about to give ShellMate.

### The certificates

The service authenticates with **mutual TLS**. There is no token and no
password: a client either presents a certificate the service trusts, or it
is refused. On first start the service generates a self-signed CA and a
client pair in its certs directory. Copy `client.crt` and `client.key` to
the machine running ShellMate and keep them as files — the key is a secret
and belongs in a file with its own permissions, which is why ShellMate
stores the *paths* rather than the contents.

### In ShellMate

Open **Settings → Ansible** and fill in:

| Field | What it is |
|---|---|
| Runner URL | `https://runner.example:5001` — HTTPS, always |
| Client certificate | The `client.crt` you copied |
| Certificate key | The matching `client.key` |
| CA certificate | The service's CA, to verify it is the runner you meant |
| Verify TLS | Leave on with a CA; turn off only for the self-signed development certificate |
| Project directory | Where playbooks live inside the container |

**Test connection** asks the runner what it holds and tells you what came
back, or exactly what failed. A missing certificate file is named as a
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

**The runner's own** are whatever is in its project directory. They are
listed, they can be run, and they are what a team keeping playbooks in git
will use.

**Yours** are a library kept with your ShellMate data, editable in the
panel. YAML is checked when you save, so a mistake is caught here with the
parser's own message rather than three steps later when a run refuses to
start.

### Getting yours to the runner

The service's API can list and run playbooks but has no endpoint that
accepts one. So **Send to the runner** copies the file into the container's
project directory over an SSH session ShellMate already has to that
machine: pick the session, and the panel says where the file landed.

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

**Push to the runner** writes those hosts and groups into the runner's own
inventory, one call per host because that is what the API offers. It
reports what went and what did not. Nothing is pushed until you ask.

## Reporting

A finished run can be saved as a report: what ran, against what, which
hosts changed, which failed and why. It is the same shape as the session
reports, so it can go into a change record or a ticket.

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

**Hosts refused by the limit.** The service checks a host list against its
own inventory before it starts, so a run limited to hosts it has never
heard of is refused. Push the inventory first.
