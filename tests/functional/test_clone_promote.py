"""Clone and promote semantics (#13, #25).

The important assertion is not that promote succeeds but that it **moves
the origin snapshot onto the clone and reverses the dependency**. #13 was
written believing promote severs the dependency; it does not, and the
driver's decision never to call it rests entirely on that. If a future
release changes the behaviour, these fail.
"""

import pytest

from truenas_cinder_driver import api_client


def _origin(client, pool, name):
    return client.get_zvol(pool, name).get("origin", {}).get("rawvalue")


def _snaps(client, dataset):
    return [s["id"] for s in client.get_snapshot_list(dataset)]


@pytest.fixture
def cloned(client, pool, zvol, names, cleanup):
    """A zvol, a snapshot of it, and a clone of that snapshot."""
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)
    snapshot_id = client.snapshot_id(pool, zvol, names.snapshot)
    client.clone_snapshot(snapshot_id, pool, names.clone)
    # Before the zvol fixture's own teardown, and destroying a clone is
    # always allowed -- it is the origin that is pinned, not the clone.
    cleanup("clone", client.delete_zvol, pool, names.clone)
    return snapshot_id


def test_the_clone_points_at_the_snapshot_it_came_from(
        client, pool, names, cloned):
    assert _origin(client, pool, names.clone) == cloned


def test_the_origin_snapshot_cannot_be_deleted_while_cloned(
        client, cloned):
    # 422/errno 22 under an `options.defer` key: the appliance is telling
    # the caller to defer the destroy, which would report success now and
    # destroy data later. delete_snapshot refuses instead.
    with pytest.raises(api_client.TrueNASAPIError):
        client.delete_snapshot(cloned)


def test_the_snapshot_names_its_dependent_clones(client, pool, names, cloned):
    # This is how the driver tells a clone-blocked delete apart from any
    # other failure, rather than parsing an undocumented errno.
    clones = client.get_snapshot(cloned)["properties"]["clones"]["value"]

    assert f"{pool}/{names.clone}" in clones


def test_a_clone_can_be_deleted_while_its_origin_lives(
        client, pool, zvol, names, cloned):
    # The reason the driver does not promote: this direction works, so
    # "clone a volume then delete the clone" -- much the commonest thing
    # anyone does -- succeeds. Promoting would invert it.
    client.delete_zvol(pool, names.clone)

    assert client.get_zvol(pool, zvol)


def test_promote_rejects_an_empty_request_body(client, pool, names, cloned):
    # An empty JSON object counts as a second positional argument:
    # "Too many arguments (expected 1, found 2)".
    from urllib.parse import quote
    dataset = quote(f"{pool}/{names.clone}", safe="")

    with pytest.raises(api_client.TrueNASAPIError):
        client._make_request(
            "POST", f"/pool/dataset/id/{dataset}/promote", json={})


def test_promote_moves_the_snapshot_and_reverses_the_dependency(
        client, pool, zvol, names, cloned):
    assert _snaps(client, f"{pool}/{zvol}") == [cloned]
    assert _snaps(client, f"{pool}/{names.clone}") == []

    client.promote_clone(pool, names.clone)

    moved = client.snapshot_id(pool, names.clone, names.snapshot)
    assert _snaps(client, f"{pool}/{zvol}") == []
    assert _snaps(client, f"{pool}/{names.clone}") == [moved]
    assert _origin(client, pool, zvol) == moved
    assert not _origin(client, pool, names.clone)

    # Put it back, or the zvol fixture cannot clean up: after promotion
    # the original is the clone, and destroying the new owner is what is
    # blocked.
    client.promote_clone(pool, zvol)
