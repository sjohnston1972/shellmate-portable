"""
seed_estate.py — Fill a data folder with an estate, for scale testing.

Everything in ShellMate has been exercised against a handful of connections.
The tree, the dashboard, the tab strip and the group counts have never met a
real estate, and the interesting failures only appear at size.

Default shape: 100 sites, each with 10 subgroups, each with 5 devices —
1,100 groups and 5,000 connections, every one pointing at one lab address and
one shared credential.

**It refuses to run against a data folder that already holds anything**, and
takes the target explicitly. Seeding five thousand connections into somebody's
real profiles.json would be its own incident, and this is exactly the sort of
script that gets run in the wrong window.

    python tools/seed_estate.py --into C:\\temp\\estate
    python tools/seed_estate.py --into C:\\temp\\small --sites 5 --subgroups 3

Then point ShellMate at it: copy the folder to ShellMate-Data beside the
executable, or run from source with that folder in place.

The credential is a *reference*, not five thousand copies — which is also the
thing the reference mechanism exists to prove at scale.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import paths  # noqa: E402

# Roles a site is made of. Deliberately the vocabulary of the estate rather
# than "group 1..10": the point is to see whether a real tree is navigable.
ROLES = [
    "core switches", "dist switches", "access switches", "firewalls",
    "wan routers", "wireless", "out of band", "load balancers",
    "voice gateways", "management",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--into", required=True,
                        help="An empty directory to build the estate in.")
    parser.add_argument("--sites", type=int, default=100)
    parser.add_argument("--subgroups", type=int, default=10)
    parser.add_argument("--devices", type=int, default=5)
    parser.add_argument("--host", default="192.168.20.16")
    parser.add_argument("--credential", default="stevecreds")
    parser.add_argument("--username", default="steven")
    parser.add_argument("--transport", default="ssh",
                        choices=("ssh", "telnet", "serial"))
    args = parser.parse_args()

    target = Path(args.into).resolve()
    if target.exists() and any(target.iterdir()):
        print(f"refusing to seed {target}: it is not empty.\n"
              f"Point --into at a new directory; this script writes thousands "
              f"of profiles and would bury anything already there.")
        return 1
    target.mkdir(parents=True, exist_ok=True)

    # Set before importing anything that resolves a path.
    paths._data_dir_cache = target
    assert paths.data_dir() == target

    from backend import groups as gm, profiles as pm

    if args.subgroups > len(ROLES):
        print(f"--subgroups is capped at {len(ROLES)} (the roles named above)")
        return 1

    started = time.monotonic()

    # One credential, referenced by everything. No password is set: the point
    # is the reference and the username, and a seeding script should not be
    # carrying a real one.
    credential = pm.save_credential_set(args.credential, args.username,
                                        "", storage="vault")
    print(f"credential set: {args.credential} ({credential['id'][:8]}) "
          f"as {args.username}")

    # Built in memory and written once, rather than through save_profile().
    #
    # Two reasons, and the first is not an optimisation. save_profile()
    # deduplicates on host, port, username and transport — which is right, and
    # is what stops the welcome screen filling with copies of the one device
    # somebody reconnects to twenty times a day. Every device here shares one
    # address on purpose, so going through it collapsed all five thousand into
    # a single profile.
    #
    # The second is that it reads and rewrites the whole file per profile:
    # 5,000 connections took 45 seconds and quadratic time.
    import json
    import uuid

    profiles = []
    group_entries = []
    total = 0

    for site_number in range(1, args.sites + 1):
        site = f"site-{site_number:03d}"
        group_entries.append(_group(site, _colour(site_number),
                                    order=site_number * 100))

        for role_index in range(args.subgroups):
            role = ROLES[role_index]
            key = f"{site}/{role}"
            group_entries.append(_group(key, _colour(site_number + role_index),
                                        order=site_number * 100 + role_index))

            for device in range(1, args.devices + 1):
                profiles.append({
                    "id": str(uuid.uuid4()),
                    "name": f"{site}-{role.split()[0][:3]}-{device:02d}",
                    "hostname": args.host,
                    "port": 22,
                    "connection_type": args.transport,
                    "username": args.username,
                    # Both the site and the role, because membership overlaps
                    # — the estate is browsable by either.
                    "tags": [site, key],
                    "credential_ref": credential["id"],
                })
                total += 1

        if site_number % 20 == 0:
            print(f"  {site_number:3d} sites, {total:5d} connections, "
                  f"{time.monotonic() - started:5.1f}s")

    (target / "profiles.json").write_text(
        json.dumps(profiles, indent=2), encoding="utf-8")
    (target / "groups.json").write_text(
        json.dumps(group_entries, indent=2), encoding="utf-8")

    elapsed = time.monotonic() - started
    groups = gm.list_groups()
    print(f"\n{len(groups)} groups, {total} connections in {elapsed:.1f}s")
    print(f"profiles.json is "
          f"{(target / 'profiles.json').stat().st_size / 1048576:.1f} MB")
    print(f"\nPoint ShellMate at {target}")
    return 0


def _group(key: str, colour: str, order: int) -> dict:
    """A groups.json entry. Written directly, for the reasons above."""
    import uuid

    return {"id": str(uuid.uuid4()), "key": key, "name": key,
            "colour": colour, "favourite": False, "order": order}


def _colour(n: int) -> str:
    from backend.groups import COLOURS
    return COLOURS[n % len(COLOURS)]


if __name__ == "__main__":
    raise SystemExit(main())
