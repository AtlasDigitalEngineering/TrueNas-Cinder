"""`truenas_pool` pointing at a dataset rather than a pool (#116).

The issue's central claim is that everything below the startup check
already handles a nested path — volume paths are built as
`<target>/<name>` and addressed through `/pool/dataset/id/<encoded>`,
which takes a full ZFS path. That was verified by reading the code, which
is not the same as running it.

So this drives the real lifecycle against a real dataset on a real
appliance: create, snapshot, clone, extend, export, adopt, delete. If any
of them quietly assumed the target was a pool, it fails here rather than
during a migration.

Creating the dataset goes through `_make_request`: the driver never
creates one — it validates that the operator did — so there is no
`create_dataset` on the client, and adding one for the tests' benefit
would put surface in the shipped client that nothing in production uses.
"""

import pytest

pytest.importorskip("cinder", reason="driver tests need Cinder installed")

from tests.functional.conftest import _Cfg, _Volume            # noqa: E402


@pytest.fixture
def dataset(client, pool, names, cleanup):
    """A throwaway filesystem dataset to point the driver at."""
    path = f"{pool}/{names.base}-ds"
    client._make_request("POST", "/pool/dataset",
                         json={"name": path, "type": "FILESYSTEM"})
    cleanup(f"dataset {path}", client._make_request,
            "DELETE", f"/pool/dataset/id/{path.replace('/', '%2F')}")
    return path


@pytest.fixture
def dataset_driver(config, dataset, portals, portal_addresses, iscsi_service,
                   coordinator):
    """A driver whose `truenas_pool` is a dataset, not a pool."""
    from truenas_cinder_driver import driver as tnd

    url, key, _pool, verify_ssl = config
    cfg = _Cfg(truenas_api_url=url, truenas_api_key=key,
               truenas_pool=dataset, truenas_verify_ssl=verify_ssl,
               truenas_iscsi_portal_id=portals[0],
               truenas_iscsi_portal_addresses=portal_addresses,
               truenas_adopt_removes_export=False,
               volume_backend_name="truenas-iscsi", san_is_local=False)
    driver = tnd.TrueNASISCSIDriver(configuration=cfg)
    driver.do_setup(None)
    # The check this issue is about: it used to refuse a dataset here.
    driver.check_for_setup_error()
    return driver


def test_setup_accepts_a_dataset(dataset_driver, dataset):
    assert dataset_driver.configuration.truenas_pool == dataset
    assert dataset_driver._targets_a_dataset()


def test_setup_refuses_a_dataset_that_does_not_exist(
        config, dataset, portals, portal_addresses, iscsi_service,
        coordinator):
    from cinder import exception
    from truenas_cinder_driver import driver as tnd

    url, key, _pool, verify_ssl = config
    cfg = _Cfg(truenas_api_url=url, truenas_api_key=key,
               truenas_pool=f"{dataset}/not-created",
               truenas_verify_ssl=verify_ssl,
               truenas_iscsi_portal_id=portals[0],
               truenas_iscsi_portal_addresses=portal_addresses,
               volume_backend_name="truenas-iscsi", san_is_local=False)
    driver = tnd.TrueNASISCSIDriver(configuration=cfg)
    driver.do_setup(None)

    with pytest.raises(exception.InvalidInput) as caught:
        driver.check_for_setup_error()

    assert 'does not exist' in str(caught.value)


def test_capacity_is_the_datasets_not_the_pools(
        client, dataset_driver, dataset, pool):
    """The measurement that made this mandatory rather than cosmetic.

    A quota is the reason to point Cinder at a dataset, and it only
    helps if the scheduler sees it.
    """
    gib = 1024 ** 3
    client._make_request(
        "PUT", f"/pool/dataset/id/{dataset.replace('/', '%2F')}",
        json={"quota": 5 * gib})

    stats = dataset_driver.get_volume_stats(refresh=True)
    reported = stats['pools'][0]['free_capacity_gb']

    pool_free = next(entry['free'] for entry in client.get_pool_list()
                     if entry['name'] == pool) / gib

    assert reported <= 5, (
        f'reported {reported} GiB free against a 5 GiB quota')
    assert pool_free > reported, (
        'the pool has more free space than the quota allows, so this '
        'test cannot tell the two sources apart')


def test_the_whole_lifecycle_runs_inside_the_dataset(
        client, dataset_driver, dataset, names, cleanup):
    """Create, snapshot, clone, extend, export — all in the dataset."""
    volume = _Volume(f'{names.base}-vol', size=1)
    cleanup(f'zvol {volume.name}', client.delete_zvol, dataset, volume.name)

    dataset_driver.create_volume(volume)
    assert client.get_zvol(dataset, volume.name)['type'] == 'VOLUME'

    snapshot = type('S', (), {'name': f'{names.base}-snap',
                              'volume_name': volume.name})()
    cleanup(f'snapshot {snapshot.name}', client.delete_snapshot,
            f'{dataset}/{volume.name}@{snapshot.name}')
    dataset_driver.create_snapshot(snapshot)
    assert client.get_snapshot(f'{dataset}/{volume.name}@{snapshot.name}')

    clone = _Volume(f'{names.base}-clone', size=1)
    cleanup(f'zvol {clone.name}', client.delete_zvol, dataset, clone.name)
    dataset_driver.create_volume_from_snapshot(clone, snapshot)
    assert client.get_zvol(dataset, clone.name)['type'] == 'VOLUME'

    dataset_driver.extend_volume(volume, 2)
    grown = client.get_zvol(dataset, volume.name)['volsize']['parsed']
    assert grown == 2 * 1024 ** 3

    model = dataset_driver.create_export(None, volume,
                                         {'initiator': names.iqn})
    cleanup(f'export for {volume.name}',
            dataset_driver.remove_export, None, volume)
    group = client.get_or_create_initiator_group([names.iqn])
    cleanup(f'initiator group {group}', client._make_request,
            'DELETE', f'/iscsi/initiator/id/{group}')
    # The target is named for the volume, not the dataset path.
    assert volume.name in model['provider_location']
    assert client.get_extent_by_name(volume.name)['disk'] == (
        client.zvol_disk_path(dataset, volume.name))


def test_adoption_lands_inside_the_dataset(
        client, dataset_driver, dataset, names, cleanup):
    """A zvol made by hand in the dataset is adopted in place.

    Before this, adoption renamed into `<pool>/<volume>` — lifting the
    zvol out of whatever dataset it arrived in.
    """
    foreign = f'{names.base}-foreign'
    client.create_zvol(dataset, foreign, size_gb=1)
    cleanup(f'zvol {foreign}', client.delete_zvol, dataset, foreign)

    adopted = _Volume(f'{names.base}-adopted')
    cleanup(f'zvol {adopted.name}', client.delete_zvol, dataset,
            adopted.name)

    size = dataset_driver.manage_existing_get_size(
        adopted, {'source-name': f'{dataset}/{foreign}'})
    dataset_driver.manage_existing(
        adopted, {'source-name': f'{dataset}/{foreign}'})

    assert size == 1
    # Renamed within the dataset, not lifted to the pool root.
    assert client.get_zvol(dataset, adopted.name)['type'] == 'VOLUME'
    with pytest.raises(Exception):
        client.get_zvol(dataset, foreign)
