"""Reconciliation against a real appliance (#48).

The acceptance criterion is a deliberately injected orphan: create an
export, forget about it Cinder-side, and show the report finds it. The
unit tests cover the classification exhaustively against fakes; this
proves the same logic reads a real appliance correctly, including one that
has other people's work on it.

Every assertion is scoped to objects this test created, and the known-
volume set is taken from the appliance *before* injecting anything, so the
live deployment's own export is never reported as a leak.
"""

import uuid

import pytest

from truenas_cinder_driver import reconcile


@pytest.fixture
def known_volumes(client):
    """Names that look like Cinder's and already exist.

    Taken before the test injects anything. Passing these as "Cinder knows
    about them" is what keeps a real deployment's export out of the leak
    list -- the appliance under test is serving one.
    """
    names = {extent["name"] for extent in client.get_extents()
             if extent["name"].startswith("volume-")}
    names |= {target["name"] for target in client.get_targets()
              if target["name"].startswith("volume-")}
    return names


@pytest.fixture
def known_with_zvols(client, pool, known_volumes):
    """As , plus zvols already named like Cinder's."""
    names = set(known_volumes)
    names |= {zvol["name"].split("/", 1)[-1]
              for zvol in client.list_zvols(pool)
              if zvol["name"].split("/", 1)[-1].startswith("volume-")}
    return names


def test_a_healthy_appliance_reports_no_leaks(
        client, pool, known_with_zvols):
    report = reconcile.find_orphans(client, pool, known_with_zvols)

    assert not reconcile.has_leaks(report), reconcile.describe(report, pool)


def test_an_injected_orphan_export_is_found(
        client, pool, known_with_zvols, names, portals, cleanup):
    """The acceptance criterion: an export Cinder no longer knows about."""
    orphan = "volume-%s" % uuid.uuid4()
    zvol = f"{names.base}-orphan"
    client.create_zvol(pool, zvol, size_gb=1)
    cleanup(f"orphan zvol {zvol}", client.delete_zvol, pool, zvol)

    group = client.get_or_create_initiator_group([names.iqn])
    cleanup(f"initiator group {group}", client._make_request,
            "DELETE", f"/iscsi/initiator/id/{group}")
    extent = client.create_extent(client.zvol_disk_path(pool, zvol), orphan)
    cleanup(f"extent {extent}", client.delete_extent, extent)
    target = client.create_target(orphan, group, portals)
    cleanup(f"target {target}", client.delete_target, target)
    client.create_target_extent(target, extent)

    report = reconcile.find_orphans(client, pool, known_with_zvols)

    assert [t["id"] for t in report["leaked_targets"]] == [target]
    assert [e["id"] for e in report["leaked_extents"]] == [extent]
    assert reconcile.has_leaks(report)
    # The live deployment's own export must not be swept up in it.
    assert orphan in reconcile.describe(report, pool)


def test_a_half_torn_down_export_is_found(
        client, pool, known_with_zvols, names, portals, cleanup):
    """An extent that outlived its target.

    Deleting a target cascades the link and leaves the extent (#12), which
    is exactly what a `remove_export` interrupted half way looks like. The
    extent still holds the zvol open, so the next create on that name
    fails for an apparently unrelated reason.
    """
    orphan = "volume-%s" % uuid.uuid4()
    zvol = f"{names.base}-half"
    client.create_zvol(pool, zvol, size_gb=1)
    cleanup(f"half zvol {zvol}", client.delete_zvol, pool, zvol)

    group = client.get_or_create_initiator_group([names.iqn])
    cleanup(f"initiator group {group}", client._make_request,
            "DELETE", f"/iscsi/initiator/id/{group}")
    extent = client.create_extent(client.zvol_disk_path(pool, zvol), orphan)
    cleanup(f"extent {extent}", client.delete_extent, extent)
    target = client.create_target(orphan, group, portals)
    client.create_target_extent(target, extent)

    # The interruption: the target goes, the extent survives.
    client.delete_target(target)

    report = reconcile.find_orphans(client, pool, known_with_zvols | {orphan})

    # Named as ours and still has a "volume", so it is the half-built
    # case rather than a plain leak.
    assert [e["id"] for e in report["unlinked_extents"]] == [extent]


def test_a_hand_provisioned_zvol_is_a_candidate_not_a_leak(
        client, pool, known_with_zvols, names, cleanup):
    # The distinction the whole module exists for. Getting this wrong
    # would invite an operator to delete the disks they are migrating.
    zvol = f"{names.base}-handmade"
    client.create_zvol(pool, zvol, size_gb=1)
    cleanup(f"handmade zvol {zvol}", client.delete_zvol, pool, zvol)

    report = reconcile.find_orphans(client, pool, known_with_zvols)

    listed = [z["name"] for z in report["adoptable_zvols"]]
    assert f"{pool}/{zvol}" in listed
    assert f"{pool}/{zvol}" not in [z["name"] for z in report["leaked_zvols"]]
    assert not reconcile.has_leaks(report)


def test_the_rendered_report_separates_candidates_from_leaks(
        client, pool, known_with_zvols, names, cleanup):
    zvol = f"{names.base}-handmade"
    client.create_zvol(pool, zvol, size_gb=1)
    cleanup(f"handmade zvol {zvol}", client.delete_zvol, pool, zvol)

    text = reconcile.describe(
        reconcile.find_orphans(client, pool, known_with_zvols), pool)

    assert "NOT leaks" in text
    assert "No leaks found" in text
