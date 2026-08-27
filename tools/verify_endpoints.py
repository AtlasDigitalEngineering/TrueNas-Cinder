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

# Target and extent names must be lowercase alphanumerics plus '.', '-' and
# ':' -- anything else is rejected by /iscsi/target/validate_name.
TARGET_NAME = "cinder-verify-target"
EXTENT_NAME = "cinder-verify-extent"
VERIFY_IQN = "iqn.2005-03.org.open-iscsi:cinder-verify-probe"
SNAPSHOT_NAME = "cinder-verify-snap"


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


def verify_snapshots(client, pool, zvol_name):
    """Exercise the snapshot lifecycle against the appliance.

    Guards the two defects fixed in #42, both of which failed *silently*:
    the legacy ``/zfs/snapshot`` base path, and interpolating an unencoded
    snapshot id into the URL. Each produced a 404, which the client maps to
    ``TrueNASAPINotFoundError``, which an idempotent caller swallows -- so
    the broken form reported success on every call while deleting nothing.

    Both wrong forms are asserted to still be wrong, not merely unused. A
    test that only exercises the correct path cannot detect the day someone
    reintroduces the old one.

    Args:
        client: A configured TrueNASAPIClient
        pool: Pool the zvol lives in
        zvol_name: Zvol to snapshot

    Returns:
        True if every probe passed
    """
    ok = True
    dataset = f"{pool}/{zvol_name}"
    snapshot_id = client.snapshot_id(pool, zvol_name, SNAPSHOT_NAME)

    # The legacy path must stay dead. If a future release resurrects it,
    # this fires -- better than silently keeping a second code path alive.
    ok &= expect_raises(
        "legacy /zfs/snapshot is still a 404",
        TrueNASAPINotFoundError,
        lambda: client._make_request("GET", "/zfs/snapshot"),
    )

    created = check(
        "create_snapshot()",
        lambda: client.create_snapshot(dataset, SNAPSHOT_NAME),
    )
    if isinstance(created, dict):
        if created.get("id") != snapshot_id:
            print(f"  FAIL  snapshot_id() built {snapshot_id!r} but the "
                  f"appliance called it {created.get('id')!r}")
            ok = False
        else:
            print(f"  ok    snapshot_id() matches the appliance\n"
                  f"        -> {snapshot_id}")

    try:
        listed = check(
            "get_snapshot_list(dataset=...) filters to just this zvol",
            lambda: [s.get("id")
                     for s in client.get_snapshot_list(dataset=dataset)],
        )
        if listed != [snapshot_id]:
            print(f"  FAIL  expected exactly [{snapshot_id!r}], got {listed}")
            ok = False

        unfiltered = client.get_snapshot_list()
        if len(unfiltered) <= len(listed or []):
            print("  FAIL  the unfiltered list is no larger than the "
                  "filtered one -- the dataset filter may be ignored")
            ok = False
        else:
            print(f"  ok    unfiltered list is larger ({len(unfiltered)} "
                  f"snapshots, incl. boot-pool) -- filter is doing work")

        # An unencoded id becomes extra path segments and 404s. This is the
        # bug that made the old delete_snapshot a permanent no-op.
        ok &= expect_raises(
            "an UNENCODED snapshot id still 404s (why encoding is required)",
            TrueNASAPINotFoundError,
            lambda: client._make_request(
                "GET", f"/pool/snapshot/id/{snapshot_id}"),
        )

        # errno 17, not 2 -- must not be mistaken for "already gone".
        ok &= expect_raises(
            "duplicate create -> plain error, NOT NotFound (errno 17)",
            TrueNASAPIError,
            lambda: client.create_snapshot(dataset, SNAPSHOT_NAME),
            but_not=TrueNASAPINotFoundError,
        )

        # What idempotent delete_snapshot depends on.
        ok &= expect_raises(
            "DELETE a missing snapshot -> NotFound (422, errno 2)",
            TrueNASAPINotFoundError,
            lambda: client.delete_snapshot(
                client.snapshot_id(pool, zvol_name, "cinder-verify-nope")),
        )

        # A zvol with a live snapshot cannot be deleted non-recursively.
        # delete_volume needs to know this is a hard error, not a no-op.
        ok &= expect_raises(
            "zvol with a snapshot refuses a non-recursive delete",
            TrueNASAPIError,
            lambda: client.delete_zvol(pool, zvol_name, recursive=False),
            but_not=TrueNASAPINotFoundError,
        )
    finally:
        check("delete_snapshot() cleanup",
              lambda: client.delete_snapshot(snapshot_id))
        remaining = client.get_snapshot_list(dataset=dataset)
        if remaining:
            print(f"  FAIL  snapshot survived deletion: "
                  f"{[s.get('id') for s in remaining]}")
            ok = False
        else:
            print("  ok    snapshot is gone")

    return ok


def verify_iscsi_pipeline(client, pool, zvol_name):
    """Build a complete iSCSI export around a zvol, then tear it down.

    This is the #12 pipeline end to end: portal, initiator group, extent,
    target, target-extent link, service start. Every resource is removed in
    a ``finally`` block, and the iscsitarget service is returned to whatever
    state it was in beforehand.

    Two behaviours here contradicted the design spec and are asserted rather
    than merely exercised, because both would fail silently if they changed:
    a reload does not start a stopped service, and deleting either end of a
    target-extent link cascades the link.

    Args:
        client: A configured TrueNASAPIClient
        pool: Pool the zvol lives in
        zvol_name: Name of the throwaway zvol to export

    Returns:
        True if every probe passed
    """
    ok = True
    created = []                    # LIFO: (label, callable)

    service_before = client.get_iscsi_service()
    print(f"  ..    iscsitarget before: state={service_before['state']} "
          f"enable={service_before['enable']}")

    try:
        disk = client.zvol_disk_path(pool, zvol_name)
        choices = check(
            "extent disk_choices offers the zvol",
            lambda: client._make_request("GET", "/iscsi/extent/disk_choices"),
        )
        if isinstance(choices, dict) and disk not in choices:
            print(f"  FAIL  zvol_disk_path() built {disk!r}, which is not "
                  f"one of the appliance's accepted values "
                  f"{list(choices)[:4]}")
            ok = False

        portal_id = check(
            "create_portal()",
            lambda: client.create_portal(comment="cinder-verify"),
        )
        if portal_id:
            created.append((f"portal {portal_id}",
                            lambda: client._make_request(
                                "DELETE", f"/iscsi/portal/id/{portal_id}")))

        group_id = check(
            "get_or_create_initiator_group()",
            lambda: client.get_or_create_initiator_group([VERIFY_IQN]),
        )
        if group_id:
            created.append((f"initiator group {group_id}",
                            lambda: client._make_request(
                                "DELETE", f"/iscsi/initiator/id/{group_id}")))
            # The dedupe is the whole point of the method: TrueNAS applies
            # no uniqueness constraint, so a second call must not create a
            # second group.
            again = check(
                "get_or_create_initiator_group() reuses, does not duplicate",
                lambda: client.get_or_create_initiator_group([VERIFY_IQN]),
            )
            if again != group_id:
                print(f"  FAIL  expected the same group id {group_id}, got "
                      f"{again} -- a duplicate group was created")
                ok = False

        extent_id = check(
            "create_extent()", lambda: client.create_extent(disk, EXTENT_NAME),
        )
        if extent_id:
            created.append((f"extent {extent_id}",
                            lambda: client.delete_extent(extent_id)))

        ok &= expect_raises(
            "a second extent on the same zvol is refused",
            TrueNASAPIError,
            lambda: client.create_extent(disk, EXTENT_NAME + "-dup"),
            but_not=TrueNASAPINotFoundError,
        )

        target_id = None
        if portal_id and group_id:
            target_id = check(
                "create_target()",
                lambda: client.create_target(TARGET_NAME, group_id,
                                             portal_id),
            )
        if target_id:
            created.append((f"target {target_id}",
                            lambda: client.delete_target(target_id)))

        link_id = None
        if target_id and extent_id:
            link_id = check(
                "create_target_extent()",
                lambda: client.create_target_extent(target_id, extent_id),
            )
        if link_id:
            created.append((f"target-extent {link_id}",
                            lambda: client.delete_target_extent(link_id)))

        # Name-based lookup (#16). This is the authoritative teardown path,
        # so it is asserted rather than exercised. An unrecognised filter
        # field is not rejected -- the appliance answers 200 with an empty
        # list -- so a broken filter would read as "already gone" and
        # silently orphan every export.
        print("\n  Name lookup (#16)")
        found_target = check(
            "get_target_by_name() finds the target we just made",
            lambda: client.get_target_by_name(TARGET_NAME),
        )
        if not found_target or found_target.get("id") != target_id:
            print(f"  FAIL  expected target id {target_id}, got "
                  f"{found_target.get('id') if found_target else None}")
            ok = False

        found_extent = check(
            "get_extent_by_name() finds the extent we just made",
            lambda: client.get_extent_by_name(EXTENT_NAME),
        )
        if not found_extent or found_extent.get("id") != extent_id:
            print(f"  FAIL  expected extent id {extent_id}, got "
                  f"{found_extent.get('id') if found_extent else None}")
            ok = False

        # Deliberately not using check() here. It returns None when the
        # probe *raises*, and None is also the expected pass value for this
        # assertion -- so a lookup that threw would be scored as a pass.
        # An assertion whose expected value is None has to distinguish
        # "returned None" from "blew up", or it reports success on failure.
        for label, lookup in (
            ("target", client.get_target_by_name),
            ("extent", client.get_extent_by_name),
        ):
            try:
                missing = lookup("cinder-verify-no-such-name")
            except Exception as exc:                 # noqa: BLE001
                print(f"  FAIL  unknown {label} name raised "
                      f"{type(exc).__name__}: {str(exc)[:160]}")
                ok = False
            else:
                if missing is None:
                    print(f"  ok    unknown {label} name -> None "
                          f"(not a stray match)")
                else:
                    print(f"  FAIL  expected None for an unknown {label} "
                          f"name, got {missing}")
                    ok = False

        # Reload against a stopped service is a no-op that reports no error.
        # If this ever starts returning True, the docstring on
        # reload_iscsi_service() is wrong and callers may stop checking.
        if service_before["state"] == "STOPPED":
            reloaded = check(
                "reload on a STOPPED service is a silent no-op",
                client.reload_iscsi_service,
            )
            state = check("service still stopped after reload",
                          lambda: client.get_iscsi_service()["state"])
            if reloaded or state != "STOPPED":
                print("  FAIL  a reload started the service -- "
                      "reload_iscsi_service()'s docstring is now wrong")
                ok = False

        check("start_iscsi_service()", client.start_iscsi_service)
        state = check("service state after start",
                      lambda: client.get_iscsi_service()["state"])
        if state != "RUNNING":
            print(f"  FAIL  expected RUNNING after start, got {state!r}")
            ok = False
        if service_before["state"] != "RUNNING":
            created.append(("iscsitarget service (back to STOPPED)",
                            lambda: client._make_request(
                                "POST", "/service/stop",
                                json={"service": "iscsitarget"})))

        check("reload_iscsi_service() on a running service",
              client.reload_iscsi_service)

        glob = check("get_iscsi_global_config()",
                     client.get_iscsi_global_config)
        if glob and target_id:
            print(f"  ok    full IQN a Nova initiator would log in to\n"
                  f"        -> {glob['basename']}:{TARGET_NAME}")

        # Cascade: deleting the target removes the link but keeps the
        # extent. The spec claimed TrueNAS does not cascade at all.
        if target_id and link_id:
            check("DELETE target (cascade probe)",
                  lambda: client.delete_target(target_id))
            links = check("target-extent links after target delete",
                          client.get_target_extents)
            if links:
                print("  FAIL  the target-extent link survived its target -- "
                      "delete_target()'s cascade note is now wrong")
                ok = False
            extents = check("extent survives its target",
                            client.get_extents)
            if not extents:
                print("  FAIL  deleting the target destroyed the extent -- "
                      "check that delete_extents is not being sent")
                ok = False
    finally:
        print("\n  Teardown (reverse order)")
        for label, remove in reversed(created):
            try:
                remove()
                print(f"  ok    removed {label}")
            except TrueNASAPINotFoundError:
                print(f"  ok    {label} was already gone (cascaded)")
            except Exception as exc:                 # noqa: BLE001
                print(f"  FAIL  could not remove {label}: "
                      f"{type(exc).__name__}: {str(exc)[:160]}")
                ok = False

        leftovers = {
            name: client._make_request("GET", f"/iscsi/{name}")
            for name in ("targetextent", "target", "extent", "initiator",
                         "portal")
        }
        for name, rows in leftovers.items():
            if rows:
                print(f"  FAIL  /iscsi/{name} still holds {rows}")
                ok = False
        service_after = client.get_iscsi_service()
        if service_after["state"] != service_before["state"]:
            print(f"  FAIL  iscsitarget left {service_after['state']}, "
                  f"was {service_before['state']}")
            ok = False
        else:
            print(f"  ok    iscsitarget back to {service_after['state']}")

    return ok


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

    print("\niSCSI read-only probes (#12)")
    check("get_iscsi_global_config() -- basename for #17",
          client.get_iscsi_global_config)
    portals = check("get_portals()", client.get_portals)
    if portals == []:
        print("  ..    no portals configured. That is normal on a clean "
              "appliance and is why the driver cannot assume one exists.")
    check("get_initiator_groups()", client.get_initiator_groups)
    check("get_iscsi_service() state",
          lambda: {k: client.get_iscsi_service()[k]
                   for k in ("state", "enable")})
    check("validate_target_name() accepts a Cinder-style name",
          lambda: client.validate_target_name(
              "volume-4d9e1a5c-8f3b-4a21-9c77-2e6b0f1d3a84"))
    check("validate_target_name() rejects uppercase and underscores",
          lambda: client.validate_target_name("Volume_1"))

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
        lambda: client.delete_extent(999999),
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

        print(f"\nSnapshot probes (#42), on {THROWAWAY}")
        if verify_snapshots(client, pool, THROWAWAY):
            print("  ->    snapshot lifecycle verified")
        else:
            print("  ->    SNAPSHOT PROBES REPORTED FAILURES (see above)")

        print(f"\niSCSI pipeline probes (#12), exporting {THROWAWAY}")
        if verify_iscsi_pipeline(client, pool, THROWAWAY):
            print("  ->    pipeline verified and fully torn down")
        else:
            print("  ->    PIPELINE PROBES REPORTED FAILURES (see above)")
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
