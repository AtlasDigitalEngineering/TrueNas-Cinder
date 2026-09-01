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

from tests.functional.conftest import _Volume                   # noqa: E402


@pytest.fixture
def foreign_zvol(client, pool, names, cleanup, destroy_zvol):
    """A zvol created out of band, as a hand-provisioned disk would be."""
    client.create_zvol(pool, names.zvol, size_gb=1)
    cleanup(f"foreign zvol {names.zvol}", destroy_zvol, pool, names.zvol)
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


def _entry_for(entries, pool, name):
    wanted = f"{pool}/{name}"
    for entry in entries:
        if entry["reference"]["source-name"] == wanted:
            return entry
    return None


def test_the_listing_finds_an_adoptable_zvol(driver, pool, foreign_zvol):
    entries = driver.get_manageable_volumes(
        [], None, 1000, 0, ["reference"], ["asc"])

    entry = _entry_for(entries, pool, foreign_zvol)
    assert entry is not None, "the zvol under test was not listed"
    assert entry["safe_to_manage"] is True
    assert entry["reason_not_safe"] is None
    assert entry["size"] == 1


def test_a_listed_reference_is_one_adoption_accepts(
        driver, pool, foreign_zvol):
    # The listing exists to feed `cinder manage`. If the reference it
    # hands out is not one the driver parses, the feature is decorative.
    entries = driver.get_manageable_volumes(
        [], None, 1000, 0, ["reference"], ["asc"])
    entry = _entry_for(entries, pool, foreign_zvol)

    assert driver.manage_existing_get_size(
        _Volume("unused"), entry["reference"]) == 1


def test_an_exported_zvol_is_listed_unsafe_and_names_its_export(
        client, driver, pool, foreign_zvol, names, portals, cleanup):
    disk = client.zvol_disk_path(pool, foreign_zvol)
    group = client.get_or_create_initiator_group([names.iqn])
    cleanup(f"initiator group {group}", client._make_request,
            "DELETE", f"/iscsi/initiator/id/{group}")
    extent = client.create_extent(disk, names.extent)
    cleanup(f"extent {extent}", client.delete_extent, extent)
    target = client.create_target(names.target, group, portals)
    cleanup(f"target {target}", client.delete_target, target)
    client.create_target_extent(target, extent)

    entries = driver.get_manageable_volumes(
        [], None, 1000, 0, ["reference"], ["asc"])
    entry = _entry_for(entries, pool, foreign_zvol)

    assert entry["safe_to_manage"] is False
    assert f"target {target}" in entry["reason_not_safe"]
    assert f"extent {extent}" in entry["reason_not_safe"]


def test_a_volume_with_a_live_session_is_listed_as_in_use(client, driver,
                                                          pool):
    """The in-use path, against a genuinely attached volume.

    Skipped when nothing is attached. Constructing a real iSCSI session
    from here would need an initiator and root, which is #90 -- but when
    the appliance happens to be serving one, that is the strongest
    available evidence that this branch works.
    """
    sessions = client.get_iscsi_sessions()
    if not sessions:
        pytest.skip("no live iSCSI session on the appliance to observe")

    entries = driver.get_manageable_volumes(
        [], None, 1000, 0, ["reference"], ["asc"])
    unsafe = [e for e in entries
              if e["reason_not_safe"] and "in use" in e["reason_not_safe"]]

    assert unsafe, (
        f"{len(sessions)} live session(s) exist but no entry reports one; "
        f"targets in session: "
        f"{[s.get('target') for s in sessions]}")
    for entry in unsafe:
        assert entry["safe_to_manage"] is False
        assert any(s.get("initiator") in entry["reason_not_safe"]
                   for s in sessions)


def test_an_already_managed_volume_reports_its_cinder_id(
        client, driver, pool):
    # Any zvol named volume-<uuid> that Cinder claims to manage.
    entries = driver.get_manageable_volumes(
        [], None, 1000, 0, ["reference"], ["asc"])
    named = [e for e in entries
             if e["reference"]["source-name"].rsplit("/", 1)[-1]
             .startswith("volume-")]
    if not named:
        pytest.skip("no volume-<uuid> zvol on the appliance to claim")

    uuid = named[0]["reference"]["source-name"].rsplit("/volume-", 1)[-1]
    entries = driver.get_manageable_volumes(
        [{"id": uuid}], None, 1000, 0, ["reference"], ["asc"])

    entry = next(e for e in entries if e["cinder_id"] == uuid)
    assert entry["safe_to_manage"] is False
    assert "already managed" in entry["reason_not_safe"]


def test_the_snapshot_listing_points_at_its_volume(
        client, driver, pool, foreign_zvol, names, cleanup):
    client.create_snapshot(f"{pool}/{foreign_zvol}", names.snapshot)

    entries = driver.get_manageable_snapshots(
        [], None, 1000, 0, ["reference"], ["asc"])
    wanted = client.snapshot_id(pool, foreign_zvol, names.snapshot)
    entry = next((e for e in entries
                  if e["reference"]["source-name"] == wanted), None)

    assert entry is not None, "the snapshot under test was not listed"
    assert entry["source_reference"]["source-name"] == foreign_zvol
    # A ZFS snapshot has no size of its own; Cinder wants the volume's.
    assert entry["size"] == 1
