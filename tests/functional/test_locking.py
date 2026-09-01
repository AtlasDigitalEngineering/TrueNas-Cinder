"""Serialisation against a real appliance (#18).

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
"""

import threading

import pytest

pytest.importorskip("cinder", reason="driver tests need Cinder installed")


WORKERS = 6


def _race(call, workers=WORKERS):
    """Run `call` from `workers` threads released together."""
    barrier = threading.Barrier(workers)
    results, errors = [], []

    def run():
        barrier.wait()
        try:
            results.append(call())
        except Exception as exc:                              # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, errors


def test_concurrent_attaches_create_exactly_one_initiator_group(
        client, driver, names, cleanup):
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
    for group in groups:
        cleanup(f"initiator group {group['id']}", client._make_request,
                "DELETE", f"/iscsi/initiator/id/{group['id']}")

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


def test_the_lock_id_identifies_this_appliance(driver, config):
    # Locks are per appliance. Two backends pointing at different boxes
    # must not serialise against each other.
    host = config[0].split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]

    assert driver.lock_id == host.lower()
