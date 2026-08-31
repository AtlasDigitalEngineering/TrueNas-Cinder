#!/usr/bin/env python3
"""Report TrueNAS resources with no corresponding Cinder volume (#48).

Compares what the appliance holds against what Cinder believes it has, and
says what is unaccounted for. Read-only unless told otherwise.

    python3 tools/find_orphans.py                    # report
    python3 tools/find_orphans.py --delete-exports   # and clean up

Appliance credentials come from `.env`, as the functional suite's do:

    TRUENAS_API_URL, TRUENAS_API_KEY, TRUENAS_TEST_POOL

Cinder's volume list comes from its API, using the usual `OS_*`
environment. Reading Cinder's database directly is not supported: the
schema is not a public interface, and a reconciliation that silently
misreads it would report healthy volumes as leaks.

    OS_AUTH_URL, OS_PROJECT_NAME/OS_PROJECT_ID, and either
    OS_USERNAME + OS_PASSWORD, or
    OS_APPLICATION_CREDENTIAL_ID + OS_APPLICATION_CREDENTIAL_SECRET

Requires the admin role: the volume list must cover every project, or
another tenant's volumes look like leaks.

**`--delete-exports` never removes a zvol.** Targets, extents and links
are wrappers that can be rebuilt; a zvol is the disk. A zvol with no
Cinder volume may be a leak, or it may be a volume whose Cinder record was
lost -- and those are indistinguishable from here. They are reported for a
human to decide, and this script will not act on them at any flag.
"""

import argparse
import os
import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from truenas_cinder_driver import reconcile                    # noqa: E402
from truenas_cinder_driver.api_client import (                 # noqa: E402
    TrueNASAPIClient,
    TrueNASAPIError,
)


def load_env(path=".env"):
    """Read KEY=VALUE pairs from `.env`, letting the file win.

    Deliberately not `setdefault`: a stale `TRUENAS_API_URL` exported in a
    shell would otherwise outrank the file the operator just edited, and
    point a reconciliation at the wrong appliance -- which, with
    `--delete-exports`, is the difference between a report and an outage.
    """
    env_file = pathlib.Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()


def keystone_token():
    """Authenticate to Keystone, returning (token, catalog).

    Supports password and application-credential auth, because the two
    are not interchangeable: `cinder`'s own shell cannot use application
    credentials at all (#60), so anyone following our own docs will have
    a password to hand.
    """
    auth_url = os.environ["OS_AUTH_URL"].rstrip("/")
    app_id = os.environ.get("OS_APPLICATION_CREDENTIAL_ID")

    if app_id:
        identity = {
            "methods": ["application_credential"],
            "application_credential": {
                "id": app_id,
                "secret": os.environ["OS_APPLICATION_CREDENTIAL_SECRET"],
            },
        }
        body = {"auth": {"identity": identity}}
    else:
        identity = {
            "methods": ["password"],
            "password": {"user": {
                "name": os.environ["OS_USERNAME"],
                "password": os.environ["OS_PASSWORD"],
                "domain": {"name": os.environ.get(
                    "OS_USER_DOMAIN_NAME", "Default")},
            }},
        }
        scope = {"project": {
            "domain": {"name": os.environ.get(
                "OS_PROJECT_DOMAIN_NAME", "Default")},
        }}
        if os.environ.get("OS_PROJECT_ID"):
            scope["project"]["id"] = os.environ["OS_PROJECT_ID"]
            scope["project"].pop("domain")
        else:
            scope["project"]["name"] = os.environ["OS_PROJECT_NAME"]
        body = {"auth": {"identity": identity, "scope": scope}}

    response = requests.post(f"{auth_url}/auth/tokens", json=body, timeout=30)
    if response.status_code != 201:
        sys.exit(f"Keystone rejected the credentials: HTTP "
                 f"{response.status_code}: {response.text[:300]}")
    return response.headers["X-Subject-Token"], response.json()["token"]


def cinder_volume_names(backend):
    """Return the names of every Cinder volume on one backend.

    Args:
        backend: Host string, ``host@backend`` or ``host@backend#pool``

    Returns:
        Set of `volume-<uuid>` names
    """
    token, body = keystone_token()
    endpoints = [e["url"] for s in body["catalog"] if s["type"] == "volumev3"
                 for e in s["endpoints"] if e["interface"] == "public"]
    if not endpoints:
        sys.exit("No volumev3 endpoint in the service catalog.")

    headers = {"X-Auth-Token": token,
               "OpenStack-API-Version": "volume 3.70"}
    # all_tenants: another project's volumes are still this backend's
    # volumes, and omitting them would report them as leaks.
    response = requests.get(f"{endpoints[0]}/volumes/detail",
                            headers=headers, timeout=60,
                            params={"all_tenants": 1})
    if response.status_code != 200:
        sys.exit(f"Cinder refused the volume list: HTTP "
                 f"{response.status_code}: {response.text[:300]}. An admin "
                 f"role is required.")

    volumes = response.json().get("volumes", [])
    host = backend.split("#", 1)[0]
    names = set()
    for volume in volumes:
        on = (volume.get("os-vol-host-attr:host") or "").split("#", 1)[0]
        if on == host:
            names.add("volume-%s" % volume["id"])
    return names, len(volumes)


def delete_exports(client, report):
    """Remove leaked iSCSI objects. Never touches a zvol.

    Order matters: the link first, then the target, then the extent.
    Deleting either end of a link cascades it, so removing the link
    explicitly first keeps the outcome the same whichever order the
    appliance applies internally.

    Individual failures are reported and do not stop the run -- one
    target refusing to go is no reason to leave the rest behind -- but
    they are counted, because a caller that cannot tell a partial run from
    a complete one will treat both as success.

    Args:
        client: A configured TrueNASAPIClient
        report: A report from :func:`reconcile.find_orphans`

    Returns:
        ``(removed, failed)`` counts
    """
    removed = failed = 0

    def attempt(what, delete, ident, label):
        nonlocal removed, failed
        try:
            delete(ident)
        except TrueNASAPIError as exc:
            print(f"  FAILED {what} {ident}: {exc}")
            failed += 1
        else:
            print(f"  removed {what} {label}")
            removed += 1

    for link in report["dangling_links"]:
        attempt("link", client.delete_target_extent, link["id"],
                str(link["id"]))
    for target in report["leaked_targets"]:
        attempt("target", client.delete_target, target["id"],
                f"{target['id']} {target['name']!r}")
    for extent in report["leaked_extents"] + report["unlinked_extents"]:
        attempt("extent", client.delete_extent, extent["id"],
                f"{extent['id']} {extent['name']!r}")
    return removed, failed


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--backend", required=True,
        help="Cinder host string, e.g. controller@truenas-iscsi")
    parser.add_argument(
        "--delete-exports", action="store_true",
        help="remove leaked targets, extents and links. Never zvols.")
    parser.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt for --delete-exports")
    args = parser.parse_args()

    load_env()
    url = os.environ.get("TRUENAS_API_URL")
    key = os.environ.get("TRUENAS_API_KEY")
    pool = os.environ.get("TRUENAS_TEST_POOL")
    if not (url and key and pool):
        sys.exit("TRUENAS_API_URL, TRUENAS_API_KEY and TRUENAS_TEST_POOL "
                 "must be set in .env")

    client = TrueNASAPIClient(
        url, key, verify_ssl=os.environ.get("TRUENAS_VERIFY_SSL", "1") == "1")

    names, total = cinder_volume_names(args.backend)
    print(f"Cinder reports {len(names)} volume(s) on {args.backend} "
          f"(of {total} in this cloud)\n")

    report = reconcile.find_orphans(client, pool, names)
    print(reconcile.describe(report, pool))

    if not args.delete_exports:
        if reconcile.has_leaks(report):
            print("\nRe-run with --delete-exports to remove the iSCSI "
                  "objects above. Zvols are never removed by this script.")
        return 1 if reconcile.has_leaks(report) else 0

    removable = (len(report["dangling_links"]) + len(report["leaked_targets"])
                 + len(report["leaked_extents"])
                 + len(report["unlinked_extents"]))
    if not removable:
        print("\nNothing to remove.")
        return 0

    print(f"\nAbout to remove {removable} iSCSI object(s). Zvols will not "
          f"be touched.")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    print()
    removed, failed = delete_exports(client, report)
    print(f"Removed {removed} object(s).")
    if failed:
        print(f"{failed} could not be removed.")

    # Re-read rather than assume. A caller running this on a schedule
    # needs the exit code to mean "the appliance is clean now", not "the
    # deletions were attempted" -- and some leak classes, zvols and
    # duplicate initiator groups, this flag deliberately cannot clear.
    after = reconcile.find_orphans(client, pool, names)
    if reconcile.has_leaks(after):
        print("\nStill outstanding:\n")
        print(reconcile.describe(after, pool))
        return 1
    print("\nNothing outstanding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
