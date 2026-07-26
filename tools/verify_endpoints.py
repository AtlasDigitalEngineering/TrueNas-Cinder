#!/usr/bin/env python3
"""
Verify the API client's endpoints against a live TrueNAS Scale appliance.

Everything in ``api_client.py`` was originally written from a design document
rather than observed behaviour, and three of those assumptions turned out to be
wrong (see #35). This script exists so findings can be re-checked rather
than taken on trust, and so a new TrueNAS release can be re-verified cheaply.

Usage::

    cp .env.example .env     # then fill it in
    python3 tools/verify_endpoints.py            # read-only probes
    python3 tools/verify_endpoints.py --write     # also create/delete a zvol

Safety
------
This script refuses to run unless ``TRUENAS_API_URL`` and ``TRUENAS_TEST_POOL``
are both set, and it never touches anything outside the configured pool. Write
mode creates exactly one throwaway zvol and deletes it again, including on
failure.

**Never point this at the production appliance.** It holds every production VM
disk as a zvol, and those are the migration's only copy of that data.
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from truenas_cinder_driver.api_client import (  # noqa: E402
    TrueNASAPIClient,
)

THROWAWAY = "cinder-verify-throwaway"


def load_env(path=".env"):
    """Read KEY=VALUE pairs from a .env file into os.environ."""
    env_file = pathlib.Path(path)
    if not env_file.exists():
        sys.exit(
            f"{path} not found. Copy .env.example to .env and fill it in."
        )
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def check(label, fn):
    """Run a probe, reporting the outcome without aborting the run."""
    try:
        result = fn()
    except Exception as exc:                     # noqa: BLE001
        print(f"  FAIL  {label}\n        {type(exc).__name__}: "
              f"{str(exc)[:200]}")
        return None
    rendered = json.dumps(result, default=str)
    if len(rendered) > 200:
        rendered = rendered[:200] + "..."
    print(f"  ok    {label}\n        -> {rendered}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="also create and delete a throwaway zvol in the test pool",
    )
    args = parser.parse_args()

    load_env()
    url = os.environ.get("TRUENAS_API_URL")
    key = os.environ.get("TRUENAS_API_KEY")
    pool = os.environ.get("TRUENAS_TEST_POOL")
    verify_ssl = os.environ.get("TRUENAS_VERIFY_SSL", "1") == "1"

    if not url or not key or not pool:
        sys.exit(
            "TRUENAS_API_URL, TRUENAS_API_KEY and TRUENAS_TEST_POOL must all "
            "be set in .env"
        )
    if pool == "CHANGEME" or "CHANGEME" in url:
        sys.exit("Refusing to run: .env still contains CHANGEME placeholders.")

    client = TrueNASAPIClient(url, key, verify_ssl=verify_ssl)

    print(f"Target : {url}")
    print(f"Pool   : {pool}")
    print(f"Mode   : {'read-write' if args.write else 'read-only'}\n")

    print("Read-only probes")
    version = check(
        "system version",
        lambda: client._make_request("GET", "/system/version"),
    )
    check("auth accepted (get_pool_list)", client.get_pool_list)
    check(
        "EULA endpoint returns a bare boolean",
        lambda: client._make_request("GET", "/truenas/is_eula_accepted"),
    )
    check("is_eula_accepted() parses it", client.is_eula_accepted)
    check("list_zvols() filter syntax", lambda: client.list_zvols(pool))

    if not args.write:
        print("\nSkipping write probes. Re-run with --write to exercise "
              "create/resize/delete.")
        return

    print(f"\nWrite probes (throwaway zvol in {pool})")
    created = check(
        "create_zvol()",
        lambda: client.create_zvol(pool=pool, name=THROWAWAY, size_gb=1),
    )
    try:
        if created:
            volsize = created.get("volsize")
            nested = isinstance(volsize, dict)
            print(f"        volsize nested? {nested} "
                  f"(rawvalue present: {nested and 'rawvalue' in volsize})")

        check("get_zvol()", lambda: client.get_zvol(pool, THROWAWAY))
        check(
            "list_zvols() sees it",
            lambda: [z.get("name") for z in client.list_zvols(pool)],
        )
        check(
            "resize_zvol() to 2 GiB",
            lambda: client.resize_zvol(pool, THROWAWAY, new_size_gb=2)
            .get("volsize"),
        )
    finally:
        # Cleanup must run even if a probe above raised.
        check(
            "delete_zvol() cleanup",
            lambda: client.delete_zvol(pool, THROWAWAY),
        )
        remaining = [
            z.get("name") for z in (client.list_zvols(pool) or [])
        ]
        print(f"  ok    volumes remaining in {pool}: {remaining}")

    if version:
        print(f"\nVerified against {version}. Record findings on #35.")


if __name__ == "__main__":
    main()
