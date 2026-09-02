"""Reconciliation of appliance state against Cinder's (#48).

No Cinder import, so these run in the dependency-free environment. The
module under test is deliberately free of driver imports for that reason.

The assertions that matter most are the negative ones. A reconciliation
that over-reports is worse than none: it puts objects somebody else owns
in front of a delete flag, and it teaches its reader to skim.
"""

import unittest
from unittest import mock

from truenas_cinder_driver import reconcile


VOL = "volume-4d9e1a5c-8f3b-4a21-9c77-2e6b0f1d3a84"
GONE = "volume-1111aaaa-2222-3333-4444-555566667777"
POOL = "Dev-Pool"


def _client(targets=(), extents=(), links=(), zvols=(), groups=()):
    client = mock.MagicMock()
    client.get_targets.return_value = list(targets)
    client.get_extents.return_value = list(extents)
    client.get_target_extents.return_value = list(links)
    client.list_zvols.return_value = list(zvols)
    client.get_initiator_groups.return_value = list(groups)
    return client


def _zvol(name):
    return {"name": f"{POOL}/{name}"}


class TestNothingOutstanding(unittest.TestCase):
    """A healthy appliance reports nothing."""

    def test_a_matched_export_is_not_reported(self):
        client = _client(
            targets=[{"id": 1, "name": VOL}],
            extents=[{"id": 2, "name": VOL, "disk": f"zvol/{POOL}/{VOL}"}],
            links=[{"id": 3, "target": 1, "extent": 2}],
            zvols=[_zvol(VOL)])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertFalse(reconcile.has_leaks(report))
        for name in reconcile.ORPHAN_CLASSES:
            self.assertEqual(report[name], [], name)


class TestLeaks(unittest.TestCase):
    """Objects this driver made that Cinder no longer knows about."""

    def test_a_target_with_no_cinder_volume_is_leaked(self):
        client = _client(targets=[{"id": 1, "name": GONE}])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([t["id"] for t in report["leaked_targets"]], [1])
        self.assertTrue(reconcile.has_leaks(report))

    def test_an_extent_with_no_cinder_volume_is_leaked(self):
        client = _client(extents=[{"id": 2, "name": GONE, "disk": "x"}])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([e["id"] for e in report["leaked_extents"]], [2])

    def test_an_extent_with_no_link_is_a_half_built_export(self):
        # Deleting a target cascades the link and leaves the extent (#12),
        # so this is what a failed teardown looks like. The extent still
        # holds the zvol open and blocks a later create on the same name.
        client = _client(
            extents=[{"id": 2, "name": VOL, "disk": "x"}])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([e["id"] for e in report["unlinked_extents"]], [2])

    def test_a_link_to_a_missing_end_is_dangling(self):
        client = _client(links=[{"id": 3, "target": 99, "extent": 98}])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([link["id"] for link in report["dangling_links"]],
                         [3])

    def test_a_link_with_only_its_target_missing_is_dangling(self):
        # Either end missing is enough. Deleting either end cascades the
        # link (#12), so a half-dangling link should not arise -- which is
        # exactly why it is asserted: if one ever does, it is a state
        # nothing else in this driver expects.
        client = _client(extents=[{"id": 2, "name": VOL, "disk": "x"}],
                         links=[{"id": 3, "target": 99, "extent": 2}])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([link["id"] for link in report["dangling_links"]],
                         [3])

    def test_a_link_with_only_its_extent_missing_is_dangling(self):
        client = _client(targets=[{"id": 1, "name": VOL}],
                         links=[{"id": 3, "target": 1, "extent": 98}])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([link["id"] for link in report["dangling_links"]],
                         [3])

    def test_a_zvol_with_no_cinder_volume_is_leaked(self):
        client = _client(zvols=[_zvol(GONE)])

        report = reconcile.find_orphans(client, POOL, {VOL})

        self.assertEqual([z["name"] for z in report["leaked_zvols"]],
                         [f"{POOL}/{GONE}"])


class TestNotOurs(unittest.TestCase):
    """What the module must refuse to call a leak.

    Every assertion here protects something somebody else owns from being
    named in front of a delete flag.
    """

    def test_a_hand_made_zvol_is_an_adoption_candidate_not_a_leak(self):
        client = _client(zvols=[_zvol("vm-100-disk-0")])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual([z["name"] for z in report["adoptable_zvols"]],
                         [f"{POOL}/vm-100-disk-0"])
        self.assertEqual(report["leaked_zvols"], [])
        self.assertFalse(reconcile.has_leaks(report))

    def test_a_hand_made_target_is_not_reported(self):
        client = _client(targets=[{"id": 1, "name": "vm-100-disk-0"}])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(report["leaked_targets"], [])

    def test_a_hand_made_unlinked_extent_is_not_reported(self):
        # A hand-provisioned disk part way through being wired up looks
        # exactly like this. Reporting it would put it in front of
        # --delete-exports and remove configuration we never made.
        client = _client(
            extents=[{"id": 2, "name": "vm-100-disk-0", "disk": "x"}])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(report["unlinked_extents"], [])
        self.assertEqual(report["leaked_extents"], [])
        self.assertFalse(reconcile.has_leaks(report))

    def test_adoption_candidates_alone_are_not_a_failure(self):
        # A pool full of disks waiting to be migrated is the healthy
        # state this driver exists for. A check that called it a failure
        # would be ignored within a week.
        client = _client(zvols=[_zvol(f"vm-{n}") for n in range(50)])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(len(report["adoptable_zvols"]), 50)
        self.assertFalse(reconcile.has_leaks(report))


class TestInitiatorGroups(unittest.TestCase):
    """Groups, including the #18 race made visible."""

    def test_duplicate_groups_are_reported_together(self):
        # get_or_create_initiator_group deduplicates on this side because
        # TrueNAS enforces no uniqueness, and it races under concurrent
        # attach (#18). Duplicates are that race, observed.
        client = _client(groups=[
            {"id": 1, "initiators": ["iqn.a"]},
            {"id": 2, "initiators": ["iqn.a"]},
        ])

        report = reconcile.find_orphans(client, POOL, set())

        # One row per cluster, shaped like every other class in the report
        # so that `describe`'s renderers all take a row (#97).
        self.assertEqual(report["duplicate_initiator_groups"],
                         [{"ids": [1, 2], "initiators": ["iqn.a"]}])
        self.assertTrue(reconcile.has_leaks(report))

    def test_a_duplicate_cluster_renders_like_its_siblings(self):
        # The renderer used to take the cluster while every other renderer
        # took a row, so `describe` had one entry that could not be called
        # the same way as the rest.
        client = _client(groups=[
            {"id": 1, "initiators": ["iqn.a"]},
            {"id": 2, "initiators": ["iqn.a"]},
        ])
        report = reconcile.find_orphans(client, POOL, set())

        rendered = reconcile.describe(report, POOL)

        self.assertIn("groups 1, 2 all hold ['iqn.a']", rendered)

    def test_three_groups_holding_one_iqn_are_one_cluster(self):
        # Not three rows: being a duplicate is a property of the set, and
        # there is no basis for calling any one of them the original.
        client = _client(groups=[
            {"id": 1, "initiators": ["iqn.a"]},
            {"id": 2, "initiators": ["iqn.a"]},
            {"id": 3, "initiators": ["iqn.a"]},
        ])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(report["duplicate_initiator_groups"],
                         [{"ids": [1, 2, 3], "initiators": ["iqn.a"]}])

    def test_distinct_groups_are_not_duplicates(self):
        client = _client(groups=[
            {"id": 1, "initiators": ["iqn.a"]},
            {"id": 2, "initiators": ["iqn.b"]},
        ])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(report["duplicate_initiator_groups"], [])

    def test_groups_are_only_called_unused_when_hosts_are_supplied(self):
        # Without knowing which hosts still attach, "unused" would be a
        # guess -- and a guess that names a group in front of an operator.
        client = _client(groups=[{"id": 1, "initiators": ["iqn.old"]}])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(report["unused_initiator_groups"], [])

    def test_a_group_no_known_host_uses_is_reported(self):
        client = _client(groups=[
            {"id": 1, "initiators": ["iqn.old"]},
            {"id": 2, "initiators": ["iqn.live"]},
        ])

        report = reconcile.find_orphans(
            client, POOL, set(), connector_iqns={"iqn.live"})

        self.assertEqual([g["id"] for g in report["unused_initiator_groups"]],
                         [1])

    def test_an_unused_group_is_not_counted_as_a_leak(self):
        # Nothing is broken: a decommissioned compute node leaves one
        # behind, and removing it is a decision, not a repair.
        client = _client(groups=[{"id": 1, "initiators": ["iqn.old"]}])

        report = reconcile.find_orphans(
            client, POOL, set(), connector_iqns={"iqn.live"})

        self.assertEqual(len(report["unused_initiator_groups"]), 1)
        self.assertFalse(reconcile.has_leaks(report))


class TestNaming(unittest.TestCase):
    """Ownership is decided by the configured name template."""

    def test_a_custom_prefix_is_honoured(self):
        client = _client(targets=[{"id": 1, "name": "cinder-abc"}])

        report = reconcile.find_orphans(client, POOL, set(),
                                        volume_prefix="cinder-")

        self.assertEqual([t["id"] for t in report["leaked_targets"]], [1])

    def test_the_default_prefix_does_not_claim_a_custom_named_object(self):
        client = _client(targets=[{"id": 1, "name": "cinder-abc"}])

        report = reconcile.find_orphans(client, POOL, set())

        self.assertEqual(report["leaked_targets"], [])


class TestDescribe(unittest.TestCase):
    """The rendered report."""

    def test_a_clean_report_says_so(self):
        client = _client()

        text = reconcile.describe(
            reconcile.find_orphans(client, POOL, set()), POOL)

        self.assertIn("No leaks found", text)

    def test_adoption_candidates_are_labelled_as_not_problems(self):
        client = _client(zvols=[_zvol("vm-100-disk-0")])

        text = reconcile.describe(
            reconcile.find_orphans(client, POOL, set()), POOL)

        self.assertIn("NOT leaks", text)
        self.assertIn("not problems", text)

    def test_a_leak_is_named_with_its_id(self):
        client = _client(targets=[{"id": 7, "name": GONE}])

        text = reconcile.describe(
            reconcile.find_orphans(client, POOL, {VOL}), POOL)

        self.assertIn("target 7", text)
        self.assertNotIn("No leaks found", text)
