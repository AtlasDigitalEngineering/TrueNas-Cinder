"""Adoption against a real appliance, through the real driver (#20, #25).

Skipped unless Cinder is importable, so the client-level suite still runs
in the dependency-free environment. Everything here drives
`TrueNASISCSIDriver` itself rather than the API client, because the
behaviour worth proving -- that the safety gate refuses the right export
and only that one -- lives in the driver.

The end-to-end proof through Nova is recorded on #20. This is the same
ground at a level where a failure says which call went wrong.
"""

import pytest

pytest.importorskip("cinder", reason="driver tests need Cinder installed")

from cinder import exception                                    # noqa: E402

from truenas_cinder_driver import driver as tnd                 # noqa: E402


class _Cfg(object):
    def __init__(self, **kw):
        self._v = dict(kw)

    def append_config_values(self, opts):
        for opt in opts:
            self._v.setdefault(opt.name, opt.default)

    def safe_get(self, name):
        return self._v.get(name)

    def __getattr__(self, name):
        try:
            return self.__dict__["_v"][name]
        except KeyError:
            raise AttributeError(name)


class _Volume(object):
    def __init__(self, name, size=1):
        self.name = name
        self.size = size


@pytest.fixture
def driver(config, pool, request, portals, portal_addresses, iscsi_service):
    """A driver whose setup validation has run against the appliance.

    Depends on `portals` and `iscsi_service` because
    `check_for_setup_error` requires both -- a fresh appliance has zero
    portals and a STOPPED service, and without provisioning them this
    module would error at fixture setup rather than run.

    `truenas_iscsi_portal_id` is set explicitly rather than left to
    discovery: the driver refuses to guess when an appliance has several
    portals, and a shared appliance may well have several.
    """
    url, key, _pool, verify_ssl = config
    adopt_removes = getattr(request, "param", False)
    cfg = _Cfg(truenas_api_url=url, truenas_api_key=key, truenas_pool=pool,
               truenas_verify_ssl=verify_ssl,
               truenas_iscsi_portal_id=portals[0],
               truenas_iscsi_portal_addresses=portal_addresses,
               truenas_adopt_removes_export=adopt_removes,
               volume_backend_name="truenas-iscsi", san_is_local=False)
    d = tnd.TrueNASISCSIDriver(configuration=cfg)
    d.do_setup(None)
    # Not merely setup: this is the check that fails loudly when the
    # appliance is misconfigured, and it has to pass before anything below
    # means anything.
    d.check_for_setup_error()
    return d


@pytest.fixture
def foreign_zvol(client, pool, names, cleanup):
    """A zvol created out of band, as a hand-provisioned disk would be."""
    client.create_zvol(pool, names.zvol, size_gb=1)
    cleanup(f"foreign zvol {names.zvol}", client.delete_zvol, pool,
            names.zvol)
    return names.zvol


def test_sizing_reads_the_zvol_rather_than_guessing(
        driver, pool, foreign_zvol):
    ref = {"source-name": f"{pool}/{foreign_zvol}"}

    assert driver.manage_existing_get_size(_Volume("unused"), ref) == 1


@pytest.fixture
def filesystem(client, pool, names, cleanup):
    """A child *filesystem* dataset, which is not adoptable."""
    dataset = f"{pool}/{names.base}-fs"
    client._make_request("POST", "/pool/dataset",
                         json={"name": dataset, "type": "FILESYSTEM"})
    cleanup(f"filesystem {dataset}", client.delete_zvol, pool,
            f"{names.base}-fs")
    return f"{names.base}-fs"


def test_a_reference_to_a_filesystem_is_refused(driver, pool, filesystem):
    """The `GET` on a filesystem answers 200 hazard (AGENTS.md).

    It has to be a real `pool/<name>` path resolving to a non-VOLUME
    dataset. A bare pool name would be rejected by the pool-prefix check
    long before `_adoptable_zvol` ever looked at `type`, and would pass
    this test while exercising nothing -- same exception, wrong branch.
    """
    ref = {"source-name": f"{pool}/{filesystem}"}

    with pytest.raises(exception.ManageExistingInvalidReference) as caught:
        driver.manage_existing_get_size(_Volume("unused"), ref)

    # Assert the reason, not just the type. Every rejection here raises
    # the same exception, so only the message distinguishes which check
    # actually fired.
    assert "FILESYSTEM" in str(caught.value)


def test_a_reference_outside_the_pool_is_refused(driver, pool):
    ref = {"source-name": "cinder-func-other-pool/some-zvol"}

    with pytest.raises(exception.ManageExistingInvalidReference) as caught:
        driver.manage_existing_get_size(_Volume("unused"), ref)

    assert "cinder-func-other-pool" in str(caught.value)


def test_a_reference_to_a_missing_zvol_is_refused(driver, pool):
    ref = {"source-name": f"{pool}/cinder-func-absent"}

    with pytest.raises(exception.ManageExistingInvalidReference):
        driver.manage_existing_get_size(_Volume("unused"), ref)


def test_adoption_renames_without_moving_data(
        client, driver, pool, foreign_zvol, names, cleanup):
    before = client.get_zvol(pool, foreign_zvol)["creation"]["rawvalue"]
    adopted = _Volume(f"{names.base}-adopted")
    ref = {"source-name": f"{pool}/{foreign_zvol}"}

    driver.manage_existing(adopted, ref)
    cleanup("adopted zvol", client.rename_zvol, pool, adopted.name,
            foreign_zvol)

    after = client.get_zvol(pool, adopted.name)["creation"]["rawvalue"]
    assert after == before, "creation changed: that is a copy, not a rename"


def test_an_exported_zvol_is_refused_and_names_only_its_own_export(
        client, driver, pool, foreign_zvol, names, portals, cleanup):
    """The gate that protects production data.

    Both halves matter. Refusing is not enough on its own -- the message
    has to name the export blocking *this* adoption, because an operator
    deleting the wrong one on our say-so is the failure this guards.
    """
    disk = client.zvol_disk_path(pool, foreign_zvol)
    group = client.get_or_create_initiator_group([names.iqn])
    cleanup(f"initiator group {group}", client._make_request,
            "DELETE", f"/iscsi/initiator/id/{group}")
    extent = client.create_extent(disk, names.extent)
    cleanup(f"extent {extent}", client.delete_extent, extent)
    target = client.create_target(names.target, group, portals)
    cleanup(f"target {target}", client.delete_target, target)
    client.create_target_extent(target, extent)

    with pytest.raises(exception.ManageExistingInvalidReference) as caught:
        driver.manage_existing(_Volume("volume-unused"),
                               {"source-name": f"{pool}/{foreign_zvol}"})

    message = str(caught.value)
    assert f"target {target}" in message
    assert f"extent {extent}" in message

    # Nothing else on the appliance may be named. An operator following
    # this message must not be pointed at somebody else's export.
    for other in client.get_extents():
        if other["id"] != extent:
            assert f"extent {other['id']}" not in message


@pytest.mark.parametrize("driver", [True], indirect=True)
def test_the_export_is_removed_when_the_option_allows_it(
        client, driver, pool, foreign_zvol, names, portals, cleanup):
    disk = client.zvol_disk_path(pool, foreign_zvol)
    group = client.get_or_create_initiator_group([names.iqn])
    cleanup(f"initiator group {group}", client._make_request,
            "DELETE", f"/iscsi/initiator/id/{group}")
    extent = client.create_extent(disk, names.extent)
    # Registered even though manage_existing is expected to remove these:
    # if it raises partway through, or an assertion below fails, nothing
    # else would. The cleanup fixture swallows "already gone", so this is
    # a no-op once the driver has legitimately removed them.
    cleanup(f"extent {extent}", client.delete_extent, extent)
    target = client.create_target(names.target, group, portals)
    cleanup(f"target {target}", client.delete_target, target)
    client.create_target_extent(target, extent)
    adopted = _Volume(f"{names.base}-adopted")

    driver.manage_existing(adopted, {"source-name": f"{pool}/{foreign_zvol}"})
    cleanup("adopted zvol", client.rename_zvol, pool, adopted.name,
            foreign_zvol)

    assert client.get_target_by_name(names.target) is None
    assert client.get_extent_by_name(names.extent) is None
    # The zvol itself is untouched by the export teardown.
    assert client.get_zvol(pool, adopted.name)["type"] == "VOLUME"


def test_unmanage_touches_nothing_on_the_appliance(
        client, driver, pool, foreign_zvol):
    before = client.get_zvol(pool, foreign_zvol)

    driver.unmanage(_Volume(foreign_zvol))

    after = client.get_zvol(pool, foreign_zvol)
    assert after["creation"]["rawvalue"] == before["creation"]["rawvalue"]
    assert after["volsize"]["parsed"] == before["volsize"]["parsed"]
