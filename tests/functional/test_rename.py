"""Rename semantics, which volume adoption depends on (#20, #25).

`manage_existing` adopts a zvol by renaming it, so every claim here is
load-bearing for the migration this driver exists for.

Three tests assert that a *wrong* form is still wrong rather than merely
unused. The unforced case matters most: if a future release starts
accepting it, `rename_zvol` is passing `force: true` where it no longer has
to, and the safety check the driver performs on the appliance's behalf
could be handed back.
"""

import pytest

from truenas_cinder_driver import api_client


def _creation(client, pool, name):
    return client.get_zvol(pool, name)["creation"]["rawvalue"]


def test_a_leaf_destination_is_rejected(client, pool, zvol, names):
    # "cannot create 'x': missing dataset name". new_name is a full path.
    with pytest.raises(api_client.TrueNASAPIError):
        client._make_request(
            "POST", "/pool/dataset/rename",
            json={"id": f"{pool}/{zvol}",
                  "data": {"new_name": names.clone, "force": True}})


def test_an_unforced_dataset_rename_is_rejected(client, pool, zvol, names):
    # The appliance refuses even on an idle dataset, which is why it
    # performs no safety check for us and the driver has to.
    with pytest.raises(api_client.TrueNASAPIError):
        client._make_request(
            "POST", "/pool/dataset/rename",
            json={"id": f"{pool}/{zvol}",
                  "data": {"new_name": f"{pool}/{names.clone}"}})


def test_an_unforced_snapshot_rename_is_rejected(client, pool, zvol, names):
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)
    snapshot_id = client.snapshot_id(pool, zvol, names.snapshot)

    with pytest.raises(api_client.TrueNASAPIError):
        client._make_request(
            "POST", "/pool/snapshot/rename",
            json={"id": snapshot_id,
                  "options": {"new_name": f"{pool}/{zvol}@renamed"}})


def test_rename_preserves_the_dataset_rather_than_copying_it(
        client, pool, zvol, names, cleanup):
    # The entire migration rests on this: a rename moves no data, so
    # adopting a 10 TiB disk costs the same as a 1 GiB one. `creation`
    # surviving byte-for-byte is the evidence.
    before = _creation(client, pool, zvol)

    client.rename_zvol(pool, zvol, names.clone)
    cleanup("renamed zvol", client.rename_zvol, pool, names.clone, zvol)

    assert _creation(client, pool, names.clone) == before


def test_the_old_name_is_gone_after_a_rename(
        client, pool, zvol, names, cleanup):
    client.rename_zvol(pool, zvol, names.clone)
    cleanup("renamed zvol", client.rename_zvol, pool, names.clone, zvol)

    with pytest.raises(api_client.TrueNASAPINotFoundError):
        client.get_zvol(pool, zvol)


def test_renaming_a_missing_source_maps_to_not_found(client, pool, names):
    # 422 with errno 2, in the *flat* body shape the rename endpoints use
    # rather than the per-field lists everything else returns (#20).
    with pytest.raises(api_client.TrueNASAPINotFoundError):
        client.rename_zvol(pool, "cinder-func-absent", names.clone)


def test_renaming_onto_an_occupied_name_is_not_reported_as_not_found(
        client, pool, zvol, names):
    # errno 14, not 2. Reading it as "already gone" would let an adoption
    # silently target a name another volume owns.
    client.create_zvol(pool, names.clone, size_gb=1)
    try:
        with pytest.raises(api_client.TrueNASAPIError) as caught:
            client.rename_zvol(pool, zvol, names.clone)

        assert not isinstance(caught.value,
                              api_client.TrueNASAPINotFoundError)
    finally:
        client.delete_zvol(pool, names.clone)


def test_a_snapshot_rename_keeps_it_on_its_dataset(
        client, pool, zvol, names):
    client.create_snapshot(f"{pool}/{zvol}", names.snapshot)
    snapshot_id = client.snapshot_id(pool, zvol, names.snapshot)

    client.rename_snapshot(snapshot_id, "renamed")

    listed = [s["id"] for s in client.get_snapshot_list(f"{pool}/{zvol}")]
    assert listed == [client.snapshot_id(pool, zvol, "renamed")]
