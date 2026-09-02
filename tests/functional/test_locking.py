"""Serialisation against a real appliance (#18, #90).

The race these locks exist for is not theoretical and not narrow. Measured
here before the fix: **six concurrent calls produced six initiator
groups**, every one of them missing the other five. TrueNAS enforces no
uniqueness on initiator groups, so the deduplication has to happen on this
side, and a read-modify-write without a lock does not deduplicate at all.

These need a real coordinator, which Cinder normally starts in the
service. The shared `coordinator` fixture starts one against a temporary
file backend, and every driver-level fixture depends on it -- taking a
lock without one raises `LockCreationFailed`, which reads as a driver bug
rather than a missing service.

The parallel-attach test at the end is #90's, and is the same race in the
shape a deployment actually meets it: one compute node exporting several
volumes at once. It runs the whole `create_export` pipeline rather than
the locked call alone, and finishes by logging in to every export it
built -- because a pipeline that races leaves configuration the appliance
accepted and no initiator can use, which raises nothing.
"""

import re
import threading

import pytest

pytest.importorskip("cinder", reason="driver tests need Cinder installed")

from tests.functional import iscsi_probe                     # noqa: E402
from tests.functional.conftest import _Volume                # noqa: E402


WORKERS = 6


def _race_each(calls):
    """Run each callable in its own thread, all released together."""
    barrier = threading.Barrier(len(calls))
    results, errors = [], []

    def run(call):
        barrier.wait()
        try:
            results.append(call())
        except Exception as exc:                              # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, errors


def _race(call, workers=WORKERS):
    """Run `call` from `workers` threads released together."""
    return _race_each([call] * workers)


def test_concurrent_attaches_create_exactly_one_initiator_group(
        client, driver, names, owned_initiator_groups):
    """#18's acceptance criterion, against the appliance.

    Unlocked, this produced one group per caller. The assertion is on the
    appliance's state rather than on the returned ids, because six calls
    all returning *an* id says nothing about how many groups exist.
    """
    results, errors = _race(lambda: driver._initiator_group_for(names.iqn))

    assert not errors, [repr(e)[:200] for e in errors]
    assert len(results) == WORKERS

    groups = [g for g in client.get_initiator_groups()
              if g.get("initiators") == [names.iqn]]

    assert len(groups) == 1, (
        f"{len(groups)} groups created for one IQN: "
        f"{[g['id'] for g in groups]}")
    # Every caller got the same one, not just "one exists".
    assert set(results) == {groups[0]["id"]}


def test_concurrent_reloads_are_serialised_without_error(
        client, driver, iscsi_service):
    """The reload is a global appliance operation.

    The pipeline around it is deliberately *not* serialised -- five
    concurrent builds were measured succeeding -- but concurrent reloads
    are the one place two callers touch the same thing, and the lock costs
    nothing because a reload is quick.
    """
    results, errors = _race(driver._reload_exports)

    assert not errors, [repr(e)[:200] for e in errors]
    assert len(results) == WORKERS
    assert client.get_iscsi_service()["state"] == "RUNNING"


def test_the_lock_id_identifies_this_appliance(driver):
    """Properties, not a second parser (#99).

    This used to hand-parse the host out of the configured URL and
    compare. A test that reimplements the thing it tests can only agree
    or disagree with it, and reproduces its bugs when it agrees — and
    this one disagreed for two real URL shapes:

        https://[fe80::1]/   driver 'fe80--1'   hand-parse '[fe80'
        http://nas_one/      driver 'nas-one'   hand-parse 'nas_one'

    So against an IPv6-addressed or underscore-named appliance the
    *test* failed, not the driver. The properties below hold for
    hostnames, IPv4 and IPv6 alike; `TestApplianceLockId` in the driver
    suite covers the exact values.
    """
    lock_id = driver.lock_id

    # Non-empty, or the lock name degenerates to a bare prefix shared by
    # every appliance — which is under-serialising, the unsafe direction.
    assert lock_id

    # tooz's file driver puts this in a path, so nothing outside the safe
    # set may survive.
    assert re.fullmatch(r'[a-z0-9.-]+', lock_id), lock_id

    # A different appliance must not share this one's lock.
    assert driver._appliance_lock_id('https://not-this-appliance') != lock_id

    # Port-stability and the exact values live in the driver suite's
    # `TestApplianceLockId`, where the inputs are literal. Deriving a
    # second URL from the configured one here would mean parsing it,
    # which is the mistake this test was rewritten to stop making --
    # the first attempt at that mangled `https://[::1]/` into
    # `https://[:8443::1]/` and raised inside `urlsplit`.


ATTACHES = 4


def test_parallel_attaches_all_succeed_and_share_one_group(
        client, driver, pool, names, cleanup, destroy_zvol,
        owned_initiator_groups):
    """#90: N volumes attached at once by one compute node.

    The realistic shape of the #18 race. A host booting several instances,
    or a live migration moving a multi-disk one, calls `create_export`
    concurrently with the *same* connector -- so every caller wants the
    same initiator group and races to create it.

    Asserted on the appliance rather than on the return values, and to the
    end of the pipeline rather than to "no exception": an export that was
    built but is not loginable is exactly the failure this suite exists to
    catch, and it raises nothing at all.
    """
    volumes = []
    for index in range(ATTACHES):
        name = "%s-%d" % (names.base, index)
        client.create_zvol(pool, name, size_gb=1)
        cleanup(f"zvol {name}", destroy_zvol, pool, name)
        volume = _Volume(name)
        # Registered before the race, not after it: `remove_export` is
        # idempotent, so covering an export that was never built costs
        # nothing, while a thread that dies part way through would
        # otherwise leak the half it did build.
        cleanup(f"export for {name}", driver.remove_export, None, volume)
        volumes.append(volume)

    connector = {'initiator': names.iqn}
    results, errors = _race_each(
        [lambda v=volume: driver.create_export(None, v, connector)
         for volume in volumes])

    assert not errors, [repr(e)[:200] for e in errors]
    assert len(results) == ATTACHES

    groups = [g for g in client.get_initiator_groups()
              if g.get("initiators") == [names.iqn]]
    assert len(groups) == 1, (
        f"{len(groups)} initiator groups for one IQN: "
        f"{[g['id'] for g in groups]}")

    # Each volume got its own target and extent, correctly paired. A race
    # that crossed two volumes' extents would export the wrong disk into
    # an instance, and every count above would still be right.
    links = {link["target"]: link["extent"]
             for link in client.get_target_extents()}
    for volume in volumes:
        target = client.get_target_by_name(volume.name)
        extent = client.get_extent_by_name(volume.name)
        assert target and extent, f"{volume.name} was not fully exported"
        assert links.get(target["id"]) == extent["id"], (
            f"{volume.name}: target {target['id']} is not linked to its "
            f"own extent {extent['id']}")
        assert extent["disk"] == client.zvol_disk_path(pool, volume.name)

    # And they are all actually usable. Everything above this line is
    # satisfied by configuration the appliance accepted but never loaded.
    for model in results:
        portals, iqn, _lun = iscsi_probe.parse_provider_location(
            model["provider_location"])
        address, port = portals[0]
        with iscsi_probe.login(address, names.iqn, iqn, port=port) as session:
            assert session["tsih"], f"no session handle for {iqn}"
