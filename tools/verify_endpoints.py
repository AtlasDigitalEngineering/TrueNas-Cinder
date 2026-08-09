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
    TrueNASAPIAuthError,
    TrueNASAPIClient,
    TrueNASAPIError,
    TrueNASAPINotFoundError,
)

THROWAWAY = "cinder-verify-throwaway"


def load_env(path=".env"):
    """Read KEY=VALUE pairs from a .env file into os.environ.

    The file wins over anything already exported. This is deliberate: with
    `setdefault`, a stale `TRUENAS_API_URL` left in the shell would silently
    override `.env` and could point a write-mode run at a different
    appliance than the one the operator just configured.
    """
    env_file = pathlib.Path(path)
    if not env_file.exists():
        sys.exit(
            f"{path} not found. Copy .env.example to .env and fill it in."
        )
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()


def check(label, fn):
    """Run a probe, reporting the outcome without aborting the run.

    Args:
        label: Human-readable description of what is being probed
        fn: Zero-argument callable performing the probe

    Returns:
        Whatever ``fn`` returned, or None if it raised
    """
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


def expect_raises(label, expected, fn, but_not=None):
    """Probe an error path, asserting which exception type comes back.

    The error mapping in ``api_client`` is the part most likely to drift on
    a TrueNAS upgrade: it depends on undocumented status codes and on the
    ``errno`` in a 422 body. A silent change here would turn an idempotent
    delete back into a hard failure, so these are checked explicitly rather
    than eyeballed.

    Args:
        label: Human-readable description of the probe
        expected: Exception class the call is required to raise
        fn: Zero-argument callable expected to raise
        but_not: Subclass of ``expected`` that must *not* be raised. Needed
            because every mapping here is a ``TrueNASAPIError``, so
            asserting the base class alone passes vacuously.

    Returns:
        True if the probe matched
    """
    try:
        result = fn()
    except expected as exc:
        if but_not is not None and isinstance(exc, but_not):
            print(f"  FAIL  {label}\n        got {type(exc).__name__}, which "
                  f"must not be a {but_not.__name__}: {str(exc)[:160]}")
            return False
        print(f"  ok    {label}\n        -> {type(exc).__name__}: "
              f"{str(exc)[:160]}")
        return True
    except Exception as exc:                     # noqa: BLE001
        print(f"  FAIL  {label}\n        expected {expected.__name__}, got "
              f"{type(exc).__name__}: {str(exc)[:160]}")
        return False
    print(f"  FAIL  {label}\n        expected {expected.__name__}, but the "
          f"call succeeded: {str(result)[:160]}")
    return False


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
    placeholders = [
        name for name, value in (
            ("TRUENAS_API_URL", url),
            ("TRUENAS_API_KEY", key),
            ("TRUENAS_TEST_POOL", pool),
        ) if "CHANGEME" in value
    ]
    if placeholders:
        sys.exit(
            "Refusing to run: .env still contains CHANGEME placeholders for "
            + ", ".join(placeholders)
        )

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

    print("\nError mapping (#11)")
    missing = "cinder-verify-does-not-exist"
    expect_raises(
        "GET missing dataset -> NotFound (404)",
        TrueNASAPINotFoundError,
        lambda: client.get_zvol(pool, missing),
    )
    # The important one. DELETE answers 422 with errno 2, not 404, so this
    # is what idempotent delete_volume actually relies on.
    expect_raises(
        "DELETE missing dataset -> NotFound (422, errno 2)",
        TrueNASAPINotFoundError,
        lambda: client.delete_zvol(pool, missing),
    )
    expect_raises(
        "PUT missing dataset -> NotFound (422, errno 2)",
        TrueNASAPINotFoundError,
        lambda: client.resize_zvol(pool, missing, new_size_gb=2),
    )
    expect_raises(
        "DELETE missing iSCSI extent -> NotFound (422, errno 2)",
        TrueNASAPINotFoundError,
        lambda: client.delete_iscsi_extent(999999),
    )
    # errno 22 with a "does not exist" message. Must NOT read as NotFound,
    # or a failed create against a misconfigured pool would be reported as
    # a successful delete. Creates nothing -- the pool does not exist -- so
    # this is safe in read-only mode.
    expect_raises(
        "create into a nonexistent pool -> plain error, NOT NotFound "
        "(422, errno 22)",
        TrueNASAPIError,
        lambda: client.create_zvol(
            pool="CinderVerifyNoSuchPool", name=missing, size_gb=1
        ),
        but_not=TrueNASAPINotFoundError,
    )

    bad_key_client = TrueNASAPIClient(
        url, "1-invalidkey", verify_ssl=verify_ssl
    )
    expect_raises(
        "bad API key -> AuthError (401)",
        TrueNASAPIAuthError,
        bad_key_client.get_pool_list,
    )

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
        if isinstance(created, dict):
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
        print(f"\nVerified against {version}. Record findings on the issue "
              f"that owns the endpoint you were checking.")


if __name__ == "__main__":
    main()
