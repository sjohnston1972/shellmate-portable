"""
ansible_keys.py — The secrets an automation needs, and where they go (#586).

A playbook run needs credentials ShellMate does not otherwise hold: an
Azure client secret, an AWS access key, a Meraki API key, a vault password,
the passphrase on the runner's SSH key. They cannot live in the playbook,
they should not be typed into the run dialog every time, and they must not
be written to ``settings.json``.

So they live in ShellMate's encrypted vault under a name, and a run refers
to them **by that name**. An environment says "this run needs
``azure_secret`` as ``AZURE_CLIENT_SECRET``"; at the moment the run starts,
the value is fetched from the vault and travels with the job. Nothing is
stored on the runner, and nothing crosses the wire until a run actually
needs it.

Two honest limits, stated here because the panel states them too:

- **The value does reach the runner.** It has to: Ansible is what uses it.
  What this buys is that the secret is not in a playbook, not in a file on
  the container, and not in a shell history — and that it goes over the
  connection ShellMate already trusts rather than being pasted somewhere.
- **The value never comes back.** Listing keys returns names, kinds and
  when they were set. There is no endpoint that reads one; the only way
  out is a run. A key you cannot remember has to be replaced, which is the
  correct trade for a store nothing can exfiltrate through the API.

Metadata (the names, what each is for) is an ordinary JSON file; only the
values are in the vault. That way the panel can list keys with the vault
locked, and say plainly which of them are currently unreadable.
"""

import logging
import re
import time
import uuid

from backend import jsonfile, paths

logger = logging.getLogger(__name__)


def _file():
    return paths.data_dir() / "ansible" / "keys.json"


class KeyError_(ValueError):
    """A key that cannot be stored as asked."""


#: How a key is delivered to the run. The distinction is not cosmetic:
#: an environment variable is visible to every task in the play, while an
#: extra var is a variable a playbook has to name to use.
DELIVERY = ("env", "extra_var")

#: What a key is for. Used only to group the list and to pick sensible
#: default variable names — nothing branches on it.
KINDS = {
    "generic": "Something else",
    "cloud": "A cloud API credential",
    "device": "A device or network credential",
    "vault": "An Ansible Vault password",
    "ssh": "An SSH key passphrase",
}

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")

#: The vault key a stored value lives under. Prefixed so an Ansible key can
#: never collide with ``ansible_token`` or a provider's API key.
_VAULT_PREFIX = "ansible_key:"


def _vault():
    from backend.vault import vault

    return vault


def _load() -> list[dict]:
    return jsonfile.read(_file(), [], expect=list)


def keys() -> list[dict]:
    """
    Every key, without a single value.

    ``readable`` says whether the vault can currently produce the value.
    A locked vault is not an error here — the list is still worth showing,
    and a run that needs an unreadable key fails with that as its reason
    rather than with an empty variable.
    """
    rows = sorted(_load(), key=lambda k: k.get("name") or "")
    for row in rows:
        row.pop("value", None)                 # belt and braces; never stored here
        row["readable"] = bool(_read_value(row.get("name", "")))
    return rows


def _read_value(name: str) -> str:
    if not name:
        return ""
    try:
        return _vault().get(_VAULT_PREFIX + name, "") or ""
    except Exception:                                       # locked, or no vault
        return ""


def save_key(fields: dict) -> dict:
    """
    Store or update a key.

    An empty ``value`` on an existing key leaves the stored value alone, so
    the panel can rename a key or change where it is delivered without the
    user having to retype a secret they may not have.
    """
    name = str(fields.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise KeyError_(
            "A key name is lower case letters, digits and underscores, "
            "starting with a letter or underscore — it becomes a variable.")

    delivery = str(fields.get("delivery") or "env")
    if delivery not in DELIVERY:
        raise KeyError_("A key is delivered as an environment variable or an extra var.")

    kind = str(fields.get("kind") or "generic")
    if kind not in KINDS:
        raise KeyError_(f"'{kind}' is not a kind of key ShellMate knows.")

    target = str(fields.get("target") or "").strip()
    if not target:
        target = name.upper() if delivery == "env" else name
    if delivery == "env" and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", target):
        raise KeyError_("An environment variable name holds letters, digits "
                        "and underscores, and cannot start with a digit.")
    if delivery == "extra_var" and not _NAME_RE.match(target):
        raise KeyError_("An extra var name is lower case letters, digits and "
                        "underscores.")

    value = str(fields.get("value") or "")
    entry_id = str(fields.get("id") or "")

    path = _file()
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        existing = next((r for r in rows if r.get("id") == entry_id), None)
        clash = next((r for r in rows
                      if r.get("name") == name and r.get("id") != entry_id), None)
        if clash:
            raise KeyError_(f"There is already a key called '{name}'.")
        if not value and existing is None:
            raise KeyError_("A new key needs a value.")

        # A rename has to move the stored value, or the key stops resolving.
        old_name = (existing or {}).get("name", "")
        carried = ""
        if existing and old_name and old_name != name and not value:
            carried = _read_value(old_name)
            if not carried:
                raise KeyError_(
                    "The vault cannot read this key's value at the moment, so "
                    "renaming it would lose it. Unlock the vault, or set a new "
                    "value with the new name.")

        entry = {
            "id": entry_id or str(uuid.uuid4()),
            "name": name,
            "kind": kind,
            "delivery": delivery,
            "target": target,
            "description": str(fields.get("description") or "").strip(),
            "updated": time.time(),
        }
        if existing is not None:
            rows[rows.index(existing)] = entry
        else:
            rows.append(entry)

        if value or carried:
            _vault().set(_VAULT_PREFIX + name, value or carried)
            if old_name and old_name != name:
                try:
                    _vault().delete(_VAULT_PREFIX + old_name)
                except Exception:
                    logger.warning("Old Ansible key value left in the vault: %s", old_name)
        jsonfile.write(path, rows)

    logger.info("Ansible key saved: %s (%s as %s)", name, delivery, target)
    entry["readable"] = bool(_read_value(name))
    return entry


def delete_key(entry_id: str) -> bool:
    """Forget a key, value and all."""
    path = _file()
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        gone = next((r for r in rows if r.get("id") == entry_id), None)
        if gone is None:
            return False
        jsonfile.write(path, [r for r in rows if r.get("id") != entry_id])
    try:
        _vault().delete(_VAULT_PREFIX + gone.get("name", ""))
    except Exception:
        logger.warning("Vault value survived a deleted Ansible key: %s", gone.get("name"))
    return True


def resolve(names: list[str]) -> tuple[dict, dict, list[str]]:
    """
    Turn key names into what a run actually carries.

    Returns the environment variables, the extra vars, and the names that
    could not be read. The caller stops on that third list rather than
    starting a run with a blank credential — Ansible's failure for an empty
    password is several screens away from the cause.
    """
    wanted = {n for n in (names or []) if n}
    env: dict[str, str] = {}
    extra: dict[str, str] = {}
    unreadable: list[str] = []
    for row in _load():
        if row.get("name") not in wanted:
            continue
        value = _read_value(row["name"])
        if not value:
            unreadable.append(row["name"])
            continue
        if row.get("delivery") == "extra_var":
            extra[row.get("target") or row["name"]] = value
        else:
            env[row.get("target") or row["name"].upper()] = value
    known = {r.get("name") for r in _load()}
    unreadable.extend(sorted(wanted - known))
    return env, extra, sorted(set(unreadable))


def count() -> int:
    try:
        return len(_load())
    except Exception:                                       # pragma: no cover
        return 0
