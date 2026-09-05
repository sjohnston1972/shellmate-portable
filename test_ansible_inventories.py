"""
test_ansible_inventories.py — Lists somebody built, rather than the estate (#608).

Most of what matters here is refusal, because the failure this feature can
produce is quiet. An inventory built from the wrong column is well-formed,
looks populated, and targets nothing — and the run that follows reports a
problem about hosts rather than about the file.

So: the host column is asked for and never guessed, a file nobody mapped is
refused, and nothing invents a platform. That last one has teeth. A wrong
`ansible_network_os` makes Ansible treat a firewall as a switch, and the
failure surfaces from a module several steps away from the cause.

The header detection is the other place to be careful. A headerless export
whose first row is a real device must not lose that device to being read as
a header — nobody notices one switch missing from a run of forty.

Run: python test_ansible_inventories.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-custinv-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import ansible_inventories as store  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def refuses(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except store.InventoryError as exc:
        return str(exc)
    return ""


MERAKI = (
    "Name,Serial,Model,MAC,LAN IP,Network\n"
    "sw-core-1,Q2AA-BBBB-CCCC,MS425-32,00:11:22:33:44:55,10.20.0.5,Site A\n"
    "sw-access-1,Q2DD-EEEE-FFFF,MS120-48,00:11:22:33:44:66,10.20.0.6,Site A\n"
)

PLAIN = "10.30.0.1\n10.30.0.2\n# the spare\n10.30.0.3\n"

HEADLESS = "10.40.0.1,core-1\n10.40.0.2,core-2\n"

ANSIBLE_STYLE = (
    "ansible_host,ansible_user,ansible_port,shellmate_name\n"
    "10.50.0.1,netops,2222,edge-1\n"
)


def reading_files() -> None:
    print("\n-- Reading what somebody uploaded --")

    plain = store.preview(PLAIN)
    check("a plain list is recognised as one", plain["kind"] == "list",
          str(plain["kind"]))
    check("and comments are not counted as hosts", plain["count"] == 3,
          f"counted {plain['count']}")

    meraki = store.preview(MERAKI, "meraki.csv")
    check("a Meraki export is read as a table", meraki["kind"] == "table",
          str(meraki["kind"]))
    check("its headers come back for the user to choose from",
          "LAN IP" in meraki["headers"], str(meraki["headers"]))
    check("and the header row is not counted as a device",
          meraki["count"] == 2, f"counted {meraki['count']}")

    # A headerless file whose first row is a real device: reading it as a
    # header loses that device, and nobody notices one switch missing from
    # a run of forty.
    headless = store.preview(HEADLESS)
    check("a headerless export keeps its first row",
          headless["count"] == 2, f"counted {headless['count']}")
    check("and its columns are named positionally",
          headless["headers"][0].startswith("column"), str(headless["headers"]))

    tabbed = store.preview("host\tsite\n10.60.0.1\tGlasgow\n")
    check("tabs work as well as commas", tabbed["kind"] == "table",
          str(tabbed["kind"]))

    check("an empty file is refused", bool(refuses(store.preview, "   \n")))


def header_detection_can_be_overruled() -> None:
    """
    The one thing left to a heuristic, and the way out of it.

    Only an IP or a dotted name counts as evidence of a data row, because a
    bare word is genuinely ambiguous: `ansible_host` is a good column
    heading and `core-1` is a good hostname. An earlier version accepted
    both and read the header of an ansible_host CSV as a device.

    That leaves headerless files of bare hostnames unsure, and the answer is
    not a cleverer guess — it is saying what was concluded and letting the
    caller overrule it, the same arrangement as the columns.
    """
    print("\n-- Whether the first row is a header --")

    styled = store.preview(ANSIBLE_STYLE)
    check("a header of field names is read as a header",
          styled["headed"] is True and "ansible_host" in styled["headers"],
          str(styled["headers"]))
    check("and the row under it is the only device",
          styled["count"] == 1, str(styled["count"]))

    check("a first row holding an address is data, not a header",
          store.preview(HEADLESS)["headed"] is False,
          "reading it as a header loses a device")

    # Bare hostnames: genuinely ambiguous, so the caller gets to say.
    bare = "core-1,Glasgow\nedge-2,Glasgow\n"
    guessed = store.preview(bare)
    check("bare names are guessed at, and the guess is reported",
          "headed" in guessed, str(guessed))
    told = store.preview(bare, headed=False)
    check("and saying otherwise is honoured",
          told["count"] == 2 and told["headed"] is False, str(told))
    check("which changes what a row maps to",
          len(store.rows_from(bare, {"host": "column 1"}, headed=False)) == 2,
          "the override has to reach the parse, not just the preview")


def mapping_is_asked_for() -> None:
    print("\n-- The host column is asked for, never guessed --")

    why = refuses(store.rows_from, MERAKI, {})
    check("a table with no mapping is refused", bool(why), "it was accepted")
    check("and the refusal says why guessing is wrong",
          "guess" in why.lower(), why)

    why = refuses(store.rows_from, MERAKI, {"host": "Nope"})
    check("a column that does not exist is refused", "Nope" in why, why)

    rows = store.rows_from(MERAKI, {"host": "LAN IP", "name": "Name"})
    check("the nominated column becomes the host",
          [r["host"] for r in rows] == ["10.20.0.5", "10.20.0.6"], str(rows))
    check("and another column can carry the name",
          rows[0]["name"] == "sw-core-1", str(rows[0]))
    check("nothing else is carried across unasked",
          "Serial" not in str(rows) and "MAC" not in str(rows), str(rows))

    full = store.rows_from(ANSIBLE_STYLE, {
        "host": "ansible_host", "user": "ansible_user",
        "port": "ansible_port", "name": "shellmate_name"})
    check("user and port map through when nominated",
          full[0]["user"] == "netops" and full[0]["port"] == 2222, str(full))

    plain = store.rows_from(PLAIN, {})
    check("a plain list needs no mapping at all", len(plain) == 3, str(plain))

    # A column that is present but empty in every row is the shape of
    # somebody picking the wrong one, and it must not produce an inventory
    # that looks fine and dials nothing.
    why = refuses(store.rows_from,
                  "name,mgmt\nsw-1,\nsw-2,\n", {"host": "mgmt"})
    check("a column that is empty in every row is refused",
          "nothing to target" in why, why)


def the_shipped_examples() -> None:
    """
    Every example ships in a shape the parser actually accepts.

    Asserted through `preview` and `rows_from` rather than by eye, because
    an example is a promise: somebody matches their own export to it. One
    that only parses because something special-cased it teaches a shape
    the real upload path refuses, and the person who followed it has no
    way to tell which of the two was wrong.

    This earned its place immediately. The plain-list example carries its
    own comment — "# the distribution layer, 12 March" — and the comma in
    that comment made the whole file read as a table, because comments
    were dropped after the delimiter was chosen rather than before.
    """
    print("\n-- The shipped examples --")

    check("there are examples to ship", len(store.EXAMPLES) >= 4,
          str(len(store.EXAMPLES)))
    check("listing them does not carry their contents",
          all("text" not in e for e in store.examples()),
          "a listing that carries five files is a listing nobody needs")

    for shipped in store.EXAMPLES:
        name = shipped["id"]
        read = store.preview(shipped["text"], shipped["filename"])
        rows = store.rows_from(shipped["text"], shipped["mapping"])
        check(f"{name}: parses and yields hosts",
              len(rows) >= 2 and all(r.get("host") for r in rows),
              str(rows))
        check(f"{name}: the count it previews is the count it produces",
              read["count"] == len(rows),
              f"previewed {read['count']}, parsed {len(rows)}")
        check(f"{name}: every mapped column exists in the file",
              all(column in (read["headers"] or [])
                  for column in shipped["mapping"].values()),
              f"{shipped['mapping']} against {read['headers']}")

    # A comment with a comma in it is a plain list, not a one-column table.
    plain = next(e for e in store.EXAMPLES if e["id"] == "plain")
    check("a commented plain list is still read as a list",
          store.preview(plain["text"])["kind"] == "list",
          "the comma in the comment decided the delimiter")
    check("and nothing but comments is refused",
          bool(refuses(store.preview, "# just a note, nothing else\n")))


def storing_them() -> None:
    print("\n-- Storing them --")

    made = store.save({"name": "Weekend upgrade",
                       "hosts": [{"host": "10.20.0.5", "name": "sw-core-1"}]})
    check("a curated list saves", made["name"] == "Weekend upgrade", str(made))
    check("the summary counts hosts rather than carrying them",
          made["hosts"] == 1, str(made))
    check("listing does not carry the rows either",
          store.inventories()[0]["hosts"] == 1, str(store.inventories()))
    check("but they can be fetched", len(store.get(made["id"])["hosts"]) == 1)

    check("a second one by the same name is refused",
          "already" in refuses(store.save, {"name": "Weekend upgrade",
                                            "hosts": [{"host": "10.0.0.9"}]}))
    check("saving with the same id updates instead",
          store.save({"id": made["id"], "name": "Weekend upgrade",
                      "hosts": [{"host": "10.20.0.5"}, {"host": "10.20.0.6"}]}
                     )["hosts"] == 2)

    check("an inventory with no hosts is refused",
          bool(refuses(store.save, {"name": "Empty", "hosts": []})),
          "an inventory that dials nothing is not an inventory")
    check("a name that is a path is refused",
          bool(refuses(store.save, {"name": "../etc/passwd",
                                    "hosts": [{"host": "10.0.0.1"}]})))


def nothing_invents_a_platform() -> None:
    print("\n-- Nothing invents a platform --")

    plain = store.save({"name": "No platform said",
                        "hosts": [{"host": "10.70.0.1"}]})
    built = store.as_inventory(plain["id"])
    vars_ = built["hostvars"]["10.70.0.1"]
    check("an uploaded host gets no network_os by default",
          "ansible_network_os" not in vars_, str(vars_))
    check("and no network_cli connection either",
          "ansible_connection" not in vars_,
          "a wrong one makes Ansible treat a firewall as a switch, and the "
          "failure arrives from a module rather than from here")

    said = store.save({"name": "Platform said", "platform": "ios",
                       "hosts": [{"host": "10.70.0.2"}]})
    vars_ = store.as_inventory(said["id"])["hostvars"]["10.70.0.2"]
    check("declaring one for the whole inventory applies it",
          vars_.get("ansible_network_os") == "cisco.ios.ios", str(vars_))
    check("and brings the right connection with it",
          vars_.get("ansible_connection") == "ansible.netcommon.network_cli",
          str(vars_))

    per_row = store.save({"name": "Per row",
                          "hosts": [{"host": "10.70.0.3", "platform": "nxos"}]})
    vars_ = store.as_inventory(per_row["id"])["hostvars"]["10.70.0.3"]
    check("a row can say what it is", "nxos" in vars_.get("ansible_network_os", ""),
          str(vars_))


def the_shape_a_run_expects() -> None:
    print("\n-- The shape a run already understands --")

    made = store.save({"name": "Site nine", "source": "upload",
                       "hosts": [{"host": "10.80.0.1", "name": "s9-1",
                                  "user": "netops", "port": 2222}]})
    built = store.as_inventory(made["id"])

    from backend.ansible import inventory_as_ini, inventory_from_estate

    estate = inventory_from_estate("")
    check("it carries the same keys as the estate builder",
          set(built) >= set(estate) - {"children"},
          f"missing {sorted(set(estate) - set(built))}")
    check("so it can be written as INI unchanged",
          "10.80.0.1" in inventory_as_ini(built), inventory_as_ini(built)[:200])
    check("the host's own name travels as a variable",
          "shellmate_name=s9-1" in inventory_as_ini(built),
          inventory_as_ini(built)[:300])
    check("and its port and user with it",
          "ansible_port=2222" in inventory_as_ini(built)
          and "ansible_user=netops" in inventory_as_ini(built),
          inventory_as_ini(built)[:300])
    check("the group is named after the inventory, sanitised",
          "site_nine" in built["groups"], str(list(built["groups"])))
    check("and it is never reported as an unknown group",
          built["group_known"] is True, str(built))

    check("asking for one that is gone is refused",
          bool(refuses(store.as_inventory, "no-such-id")))
    check("deleting works", store.delete(made["id"]) is True)
    check("and deleting one that is gone says so",
          store.delete(made["id"]) is False)


if __name__ == "__main__":
    reading_files()
    header_detection_can_be_overruled()
    mapping_is_asked_for()
    the_shipped_examples()
    storing_them()
    nothing_invents_a_platform()
    the_shape_a_run_expects()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
