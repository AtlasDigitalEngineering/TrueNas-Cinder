"""Snapshot lifecycle against a real appliance (#25).

Guards the two defects fixed in #42, both of which failed *silently*: the
legacy `/zfs/snapshot` base path, and interpolating an unencoded snapshot
id into the URL. Each produced a 404, which the client maps to
`TrueNASAPINotFoundError`, which an idempotent caller swallows -- so the
broken form reported success on every call while deleting nothing.

Both wrong forms are asserted to still be wrong, not merely unused. A test
that only exercises the correct path cannot detect the day somebody
reintroduces the old one.
"""

import pytest

from truenas_cinder_driver import api_client


def test_the_legacy_zfs_snapshot_path_is_still_gone(client):
    with pytest.raises(api_client.TrueNASAPINotFoundError):
        client._make_request("GET", "/zfs/snapshot")


def test_snapshot_id_matches_what_the_appliance_calls_it(
        client, pool, zvol, names):
    created = client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    assert created["id"] == client.snapshot_id(pool, zvol, names.snapshot)


def test_a_snapshot_is_listed_for_its_dataset(client, pool, zvol, names):
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    listed = client.get_snapshot_list(f"{pool}/{zvol}")

    assert [s["id"] for s in listed] == [
        client.snapshot_id(pool, zvol, names.snapshot)]


def test_get_snapshot_resolves_an_encoded_id(client, pool, zvol, names):
    # The id contains both '/' and '@'. Interpolated raw, the slashes
    # become extra path segments and the appliance answers 404 (#42).
    snapshot_id = client.snapshot_id(pool, zvol, names.snapshot)
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    assert client.get_snapshot(snapshot_id)["id"] == snapshot_id


def test_delete_actually_removes_it(client, pool, zvol, names):
    snapshot_id = client.snapshot_id(pool, zvol, names.snapshot)
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    client.delete_snapshot(snapshot_id)

    # Asserting absence, not just that delete returned. The bug this
    # guards reported success while deleting nothing.
    assert client.get_snapshot_list(f"{pool}/{zvol}") == []


def test_deleting_a_missing_snapshot_is_not_found(client, pool, zvol, names):
    with pytest.raises(api_client.TrueNASAPINotFoundError):
        client.delete_snapshot(client.snapshot_id(pool, zvol, "absent"))


def test_creating_a_duplicate_is_an_error_but_not_not_found(
        client, pool, zvol, names):
    # errno 17 (EEXIST), not 2. If it mapped to NotFound, an idempotent
    # caller would treat a refused create as a completed delete.
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    with pytest.raises(api_client.TrueNASAPIError) as caught:
        client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    assert not isinstance(caught.value, api_client.TrueNASAPINotFoundError)


def test_a_zvol_with_a_snapshot_refuses_a_non_recursive_delete(
        client, pool, zvol, names):
    # This is what makes delete_volume report VolumeIsBusy. Passing
    # recursive=True to get past it would destroy snapshots Cinder may not
    # know about -- a visible failure turned into data loss found at
    # restore time.
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)

    with pytest.raises(api_client.TrueNASAPIError):
        client.delete_zvol(pool, zvol)
