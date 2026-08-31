"""Zvol lifecycle against a real appliance (#25).

Ported from the `tools/verify_endpoints.py` script this suite
replaced. Several assertions here exist
because the design doc was wrong about them and the wrong form failed
*quietly* -- those are asserted rather than merely exercised, so a
regression surfaces as a failure instead of as a silently empty result.
"""

import pytest

from truenas_cinder_driver import api_client


def test_pool_is_reachable_and_reports_capacity(client, pool):
    pools = client.get_pool_list()
    names = [p["name"] for p in pools]
    assert pool in names, f"{pool!r} not among {names}"

    entry = next(p for p in pools if p["name"] == pool)
    # get_volume_stats divides these; a zero or missing size would make the
    # scheduler reject the backend outright.
    assert entry["size"] > 0
    assert entry["free"] >= 0


def test_create_reports_the_requested_size(client, pool, zvol):
    got = client.get_zvol(pool, zvol)

    assert got["type"] == "VOLUME"
    assert int(got["volsize"]["parsed"]) == 1024 ** 3


def test_created_zvol_appears_in_the_pool_listing(client, pool, zvol):
    # `name__^` is TrueNAS's startswith operator. The JSON `filters=[[...]]`
    # form answers 200 with an empty list instead of erroring, so a wrong
    # query reads as "no volumes exist" (#35).
    listed = [z["name"] for z in client.list_zvols(pool)]

    assert f"{pool}/{zvol}" in listed


def test_resize_grows_the_zvol(client, pool, zvol):
    client.resize_zvol(pool, zvol, new_size_gb=2)

    got = client.get_zvol(pool, zvol)
    assert int(got["volsize"]["parsed"]) == 2 * 1024 ** 3


def test_disk_path_matches_what_the_appliance_accepts(client, pool, zvol):
    # create_extent rejects anything else, and the rejection names `disk`
    # rather than the path, so a wrong prefix is hard to diagnose later.
    disk = client.zvol_disk_path(pool, zvol)
    choices = client._make_request("GET", "/iscsi/extent/disk_choices")

    assert disk in choices, f"{disk!r} not among {list(choices)[:5]}"


def test_a_missing_zvol_get_is_not_found(client, pool):
    with pytest.raises(api_client.TrueNASAPINotFoundError):
        client.get_zvol(pool, "cinder-func-definitely-absent")


def test_a_missing_zvol_delete_is_also_not_found(client, pool):
    # This one is 422/errno 2 rather than 404, and it is the shape
    # idempotent deletes actually depend on -- a 404-only mapping would
    # never fire here (#11).
    with pytest.raises(api_client.TrueNASAPINotFoundError):
        client.delete_zvol(pool, "cinder-func-definitely-absent")


def test_creating_into_a_missing_pool_is_not_reported_as_not_found(client):
    # errno 22 with the message "zpool (X) does not exist." Matching on the
    # message rather than the errno would score this as "already deleted"
    # and report a failed create as a successful delete.
    with pytest.raises(api_client.TrueNASAPIError) as caught:
        client.create_zvol("cinder-func-no-such-pool", "v", size_gb=1)

    assert not isinstance(caught.value, api_client.TrueNASAPINotFoundError)
