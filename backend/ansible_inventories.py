"""
ansible_inventories.py — Inventories somebody built, rather than the estate (#608).

The estate answers "everything ShellMate knows about". A run usually wants
less than that, and sometimes wants something ShellMate has never heard of:

- **Curated.** A named list picked out of the estate. "The switches I am
  upgrading this weekend" is not a group and should not have to become one
  to be targeted — making it a group changes the tree everyone else sees,
  for a list that stops mattering on Monday.
- **Uploaded.** A CSV or a plain list from somewhere else entirely. Meraki
  exports devices; an IPAM exports addresses; somebody keeps a
  spreadsheet. Those are targets even though ShellMate holds no connection
  for them.

**Columns are asked for, never guessed.** A CSV header may say `LAN IP`,
`ip_address`, `mgmt`, or nothing at all, and picking one by pattern is how
an inventory ends up looking right and targeting nothing. The upload is
parsed into rows, the caller nominates which column holds the host, and
the mapping is stored with the inventory so the same file re-uploaded
behaves the same way.

**Nothing invents a platform.** An uploaded row has no `ansible_network_os`
unless the user says what the devices are. A wrong one is worse than none:
it makes Ansible treat a firewall as a switch, and the failure arrives from
a module rather than from here.
"""

import csv
import io
import logging
import re
import time
import uuid

from backend import jsonfile, paths

logger = logging.getLogger(__name__)


def _file():
    return paths.data_dir() / "ansible" / "inventories.json"


class InventoryError(ValueError):
    """A list that cannot be stored or read as asked."""


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")

#: How a row becomes a host. `host` is the only required mapping — a row
#: with no address cannot be dialled and there is nothing to guess from.
FIELDS = ("host", "name", "user", "port", "platform")

#: What ShellMate's platform ids mean to Ansible. Shared with the estate
#: builder rather than restated, so a device uploaded as `ios` is treated
#: exactly as one ShellMate identified as `ios`.
def _network_os(platform: str) -> str:
    from backend.ansible import ANSIBLE_NETWORK_OS

    return ANSIBLE_NETWORK_OS.get((platform or "").strip().lower(), "")


def _load() -> list[dict]:
    return jsonfile.read(_file(), [], expect=list)


def _checked_name(name: str) -> str:
    text = (name or "").strip()
    if not _NAME_RE.match(text):
        raise InventoryError(
            "An inventory name may hold letters, digits, spaces, dots, dashes "
            "and underscores, and must start with a letter or a digit.")
    return text


def inventories() -> list[dict]:
    """Every custom inventory, newest name order, without their rows."""
    out = []
    for row in sorted(_load(), key=lambda i: (i.get("name") or "").lower()):
        summary = {k: v for k, v in row.items() if k != "hosts"}
        summary["hosts"] = len(row.get("hosts") or [])
        out.append(summary)
    return out


def get(inventory_id: str) -> dict | None:
    """One inventory, with its hosts."""
    return next((i for i in _load() if i.get("id") == inventory_id), None)


# ---------------------------------------------------------------------------
# Reading a file somebody uploaded
# ---------------------------------------------------------------------------
def preview(text: str, filename: str = "", headed: bool | None = None) -> dict:
    """
    Read an uploaded file into rows and columns, without deciding anything.

    Returns the headers it found, the first rows, and whether it looks like
    a plain list. It deliberately does **not** pick the host column: the
    whole point is that the user says which one it is, because a header
    called `mgmt` and one called `LAN IP` mean the same thing and a header
    called `serial` does not.
    """
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        raise InventoryError("That file is empty.")

    # Comments go first, before anything is concluded from a line.
    #
    # They used to be dropped only from a plain list, which meant a list
    # carrying its own note — "# the distribution layer, 12 March" — had a
    # comma in its first line and so was read as a table. Every address
    # then became a one-column row with no header worth nominating, and
    # the refusal that followed talked about the mapping rather than about
    # the comment that caused it. Found by shipping the example (#608).
    lines = [line for line in body.split("\n")
             if line.strip() and not line.strip().startswith("#")]
    if not lines:
        raise InventoryError("That file has nothing in it but comments.")
    delimiter = _delimiter(lines[0])

    if delimiter is None:
        # A plain list: one host per line, nothing to map.
        rows = [line.strip() for line in lines]
        return {"kind": "list", "headers": [], "rows": [[r] for r in rows[:20]],
                "count": len(rows), "filename": filename}

    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    table = [row for row in reader if any((cell or "").strip() for cell in row)]
    if not table:
        raise InventoryError("That file has no rows in it.")

    # A header is assumed only when the first row has no cell that looks
    # like an address — otherwise a headerless export loses its first
    # device, which is the kind of loss nobody notices until a run misses
    # one switch.
    first = table[0]
    if headed is None:
        headed = not any(_looks_like_host(cell) for cell in first)
    headers = [h.strip() for h in first] if headed else \
              [f"column {i + 1}" for i in range(len(first))]
    body_rows = table[1:] if headed else table

    return {"kind": "table", "headers": headers,
            "rows": [r[:len(headers)] for r in body_rows[:20]],
            "count": len(body_rows), "filename": filename, "headed": headed,
            "delimiter": delimiter}


def _delimiter(line: str) -> str | None:
    """Comma, tab or semicolon — or None for a plain list."""
    for candidate in (",", "\t", ";"):
        if candidate in line:
            return candidate
    return None


#: An address or a dotted name. Deliberately narrow: only signals that
#: cannot also be a column heading count.
_HOSTISH = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}"                    # 10.0.0.1
    r"|[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+)$")  # sw1.example.net


def _looks_like_host(cell: str) -> bool:
    """
    Whether a cell is unmistakably an address, for header detection only.

    Narrow on purpose. A bare word is ambiguous — `ansible_host` is a
    perfectly good column heading and `core-1` is a perfectly good
    hostname, and an earlier version that accepted both read the header of
    an `ansible_host` CSV as a device. Only an IP address or a dotted name
    counts, because neither is plausible as a heading.

    Where that leaves it genuinely unsure — a headerless file of bare
    hostnames — the answer is not a better guess. `preview` reports what it
    concluded and the caller can say otherwise, the same way the columns
    are confirmed rather than inferred.
    """
    text = (cell or "").strip()
    if not text or " " in text:
        return False
    return bool(_HOSTISH.match(text))


def rows_from(text: str, mapping: dict, headed: bool | None = None) -> list[dict]:
    """
    Turn an uploaded file into hosts, using the mapping the user confirmed.

    Refuses rather than guesses when no host column was nominated: a file
    parsed with the wrong column produces an inventory that looks populated
    and dials nothing.
    """
    read = preview(text, headed=headed)
    host_key = str((mapping or {}).get("host") or "").strip()

    if read["kind"] == "list":
        hosts = []
        for row in _all_rows(text, read):
            value = (row[0] or "").strip()
            if value:
                hosts.append({"host": value})
        return hosts

    if not host_key:
        raise InventoryError(
            "Say which column holds the address or hostname. ShellMate will "
            "not guess: a header called 'mgmt' and one called 'LAN IP' mean "
            "the same thing, and one called 'serial' does not.")
    if host_key not in read["headers"]:
        raise InventoryError(f"There is no column called {host_key!r}.")

    index = {name: i for i, name in enumerate(read["headers"])}
    hosts: list[dict] = []
    for row in _all_rows(text, read):
        def cell(field: str) -> str:
            column = str((mapping or {}).get(field) or "").strip()
            if not column or column not in index:
                return ""
            position = index[column]
            return (row[position] or "").strip() if position < len(row) else ""

        address = cell("host")
        if not address:
            continue
        entry = {"host": address}
        for field in ("name", "user", "platform"):
            value = cell(field)
            if value:
                entry[field] = value
        port = cell("port")
        if port.isdigit():
            entry["port"] = int(port)
        hosts.append(entry)

    if not hosts:
        raise InventoryError(
            f"No row had anything in {host_key!r}, so there is nothing to "
            "target. Check the column, or the file.")
    return hosts


def _all_rows(text: str, read: dict) -> list[list[str]]:
    body = (text or "").replace("\r\n", "\n").strip()
    lines = [line for line in body.split("\n") if line.strip()]
    if read["kind"] == "list":
        return [[line.strip()] for line in lines
                if not line.strip().startswith("#")]
    table = [row for row in csv.reader(io.StringIO(body),
                                       delimiter=read["delimiter"])
             if any((cell or "").strip() for cell in row)]
    return table[1:] if read.get("headed") else table


# ---------------------------------------------------------------------------
# Storing them
# ---------------------------------------------------------------------------
def save(fields: dict) -> dict:
    """
    Store a curated or uploaded inventory.

    ``hosts`` is a list of ``{host, name?, user?, port?, platform?}``. A
    curated one is built from the estate by the caller, which is why this
    does not care which it is — what differs is where the rows came from,
    not what they are.
    """
    name = _checked_name(fields.get("name", ""))
    hosts = []
    for raw in fields.get("hosts") or []:
        address = str((raw or {}).get("host") or "").strip()
        if not address:
            continue
        entry = {"host": address}
        for key in ("name", "user", "platform"):
            value = str(raw.get(key) or "").strip()
            if value:
                entry[key] = value
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port:
            entry["port"] = port
        hosts.append(entry)

    if not hosts:
        raise InventoryError("An inventory needs at least one host in it.")

    entry_id = str(fields.get("id") or uuid.uuid4())
    record = {
        "id": entry_id,
        "name": name,
        "description": str(fields.get("description") or "").strip(),
        "source": "upload" if fields.get("source") == "upload" else "estate",
        "filename": str(fields.get("filename") or "").strip(),
        "mapping": dict(fields.get("mapping") or {}),
        # Said once, here, so the interface does not have to work it out:
        # an uploaded row has no platform unless somebody supplied one, and
        # a run against it treats the device as a plain CLI host.
        "platform": str(fields.get("platform") or "").strip(),
        "hosts": hosts,
        "updated": time.time(),
    }

    path = _file()
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        for index, existing in enumerate(rows):
            if existing.get("id") == entry_id:
                rows[index] = record
                break
        else:
            if any((r.get("name") or "").lower() == name.lower() for r in rows):
                raise InventoryError(f"There is already an inventory called "
                                     f"'{name}'.")
            rows.append(record)
        jsonfile.write(path, rows)

    logger.info("Custom inventory saved: %s (%d host(s), from %s)",
                name, len(hosts), record["source"])
    summary = {k: v for k, v in record.items() if k != "hosts"}
    summary["hosts"] = len(hosts)
    return summary


def delete(inventory_id: str) -> bool:
    path = _file()
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        kept = [r for r in rows if r.get("id") != inventory_id]
        if len(kept) == len(rows):
            return False
        jsonfile.write(path, kept)
    return True


def as_inventory(inventory_id: str) -> dict:
    """
    A custom inventory in the shape a run already understands.

    The same keys as the estate builder produces, so everything downstream
    — the INI writer, the preview, the run — treats one exactly like the
    other rather than growing a second path.
    """
    record = get(inventory_id)
    if record is None:
        raise InventoryError("There is no such inventory.")

    from backend.ansible import ansible_group_name

    group = ansible_group_name(record["name"]) or "custom"
    hostvars: dict[str, dict] = {}
    for row in record.get("hosts") or []:
        address = row["host"]
        entry = {"ansible_host": address}
        if row.get("name"):
            entry["shellmate_name"] = row["name"]
        if row.get("user"):
            entry["ansible_user"] = row["user"]
        if row.get("port"):
            entry["ansible_port"] = int(row["port"])
        # The platform is whatever was declared, per row or for the whole
        # inventory. Nothing is inferred: a wrong network_os makes Ansible
        # treat a firewall as a switch, and the failure then arrives from a
        # module rather than from here.
        network_os = _network_os(row.get("platform") or record.get("platform"))
        if network_os:
            entry["ansible_network_os"] = network_os
            entry["ansible_connection"] = "ansible.netcommon.network_cli"
        hostvars[address] = entry

    return {
        "groups": {group: sorted(hostvars)},
        "children": {},
        "group_names": {group: record["name"]},
        "hostvars": hostvars,
        "hosts": sorted(hostvars),
        "skipped": [],
        "group_known": True,
        "group": record["name"],
        "custom": record["id"],
    }


# ---------------------------------------------------------------------------
# Worked examples
# ---------------------------------------------------------------------------
#: Files in the shapes this actually receives, shipped so somebody can see
#: what "a CSV" means here before they go looking for their own.
#:
#: They are held as text rather than as files on disk because the frozen
#: build unpacks its resources into a directory the bootloader deletes on
#: exit — a data file beside the module is one more thing to remember in
#: `build.spec`, and forgetting it fails silently at the only moment it
#: matters.
#:
#: Each one is parsed by exactly the code an upload goes through. An
#: example that only works because it was special-cased teaches a shape
#: the parser does not accept, which is worse than shipping none.
EXAMPLES = [
    {
        "id": "meraki",
        "filename": "meraki-export.csv",
        "title": "A Meraki device export",
        "note": "Exported from the dashboard. The address is in a column "
                "called LAN IP, which no pattern would find on its own — "
                "which is the whole reason the column is asked for.",
        "mapping": {"host": "LAN IP", "name": "Name"},
        "text": (
            "Name,Serial,Model,MAC,LAN IP,Network\n"
            "sw-core-1,Q2AA-BBBB-CCCC,MS425-32,00:11:22:33:44:55,10.20.0.5,Site A\n"
            "sw-core-2,Q2AA-BBBB-DDDD,MS425-32,00:11:22:33:44:56,10.20.0.6,Site A\n"
            "sw-access-1,Q2DD-EEEE-FFFF,MS120-48,00:11:22:33:44:66,10.20.0.11,Site A\n"
        ),
    },
    {
        "id": "plain",
        "filename": "addresses.txt",
        "title": "A plain list of addresses",
        "note": "One host per line and nothing else. There is no mapping to "
                "do. Lines starting with # are ignored, so a list can carry "
                "its own notes.",
        "mapping": {},
        "text": (
            "# The distribution layer, 12 March\n"
            "10.30.0.1\n"
            "10.30.0.2\n"
            "# 10.30.0.3 is out of service\n"
            "10.30.0.4\n"
        ),
    },
    {
        "id": "headed",
        "filename": "site-inventory.csv",
        "title": "A spreadsheet with headings",
        "note": "The ordinary case: somebody keeps a sheet. The headings are "
                "theirs, not Ansible's, and each one is nominated by hand.",
        "mapping": {"host": "management_ip", "name": "device",
                    "user": "login", "port": "ssh_port"},
        "text": (
            "device,management_ip,login,ssh_port,location\n"
            "edge-fw-1,10.40.0.1,netops,22,Glasgow\n"
            "edge-fw-2,10.40.0.2,netops,2222,Glasgow\n"
        ),
    },
    {
        "id": "ansible",
        "filename": "ansible-style.csv",
        "title": "Columns already named for Ansible",
        "note": "Headings that are already Ansible variable names. They are "
                "still nominated rather than matched — a heading that looks "
                "like a variable is not proof that it holds one.",
        "mapping": {"host": "ansible_host", "user": "ansible_user",
                    "port": "ansible_port", "name": "shellmate_name"},
        "text": (
            "ansible_host,ansible_user,ansible_port,shellmate_name\n"
            "10.50.0.1,netops,2222,edge-1\n"
            "10.50.0.2,netops,2222,edge-2\n"
        ),
    },
    {
        "id": "headless",
        "filename": "headerless.csv",
        "title": "An export with no headings",
        "note": "The first row is a device, not a heading. Read as a heading "
                "it would lose that switch and say so nowhere, so the "
                "conclusion is shown as a tick box you can overrule.",
        "mapping": {"host": "column 1", "name": "column 2"},
        "text": (
            "10.60.0.1,core-1\n"
            "10.60.0.2,core-2\n"
            "10.60.0.3,core-3\n"
        ),
    },
]


def examples() -> list[dict]:
    """The shipped examples, without their contents."""
    return [{k: v for k, v in e.items() if k != "text"} for e in EXAMPLES]


def example(example_id: str) -> dict | None:
    """One example, with its text."""
    return next((e for e in EXAMPLES if e["id"] == example_id), None)


def count() -> int:
    try:
        return len(_load())
    except Exception:                                     # pragma: no cover
        return 0
