"""
Unit tests for the TrueNAS API client.

Every test here runs fully offline. `TrueNASClientTestCase` installs a
network guard that turns any real HTTP request into an explicit assertion
failure, so a mock that has stopped working surfaces immediately instead of
hanging on DNS or silently depending on runner network policy.

These tests pin the client's *current* behaviour. The auth scheme is known to
be wrong (issue #10) and will change; the assertions here are expected to
change with it. Zvol operations were migrated to ``/pool/dataset`` in #9.

Coverage is deliberately partial. These methods are untested here because the
issues that rewrite them carry their own test requirements, and pinning the
current shapes would only create churn:

- ``get_iscsi_target_list``, ``create_iscsi_extent``, ``delete_iscsi_extent``,
  ``delete_iscsi_target``, ``delete_iscsi_target_extent`` -- rewritten by #12
  (iSCSI pipeline), which replaces the extent ``path``/``type`` shapes.
- ``get_snapshot_list``, ``create_snapshot``, ``delete_snapshot`` -- rewritten
  by #13 (snapshot/clone), which changes the signatures from opaque ``id`` and
  pre-joined ``dataset`` strings to ``pool``/``zvol``/``snap`` components.

Add coverage as part of those issues, not by pinning today's behaviour.
"""

import unittest
from unittest import mock

import requests

from truenas_cinder_driver.api_client import TrueNASClient


BASE_URL = "https://truenas.example.com:443/api/v2.0"


def _fail_on_real_request(*args, **kwargs):
    """Transport adapter stand-in that refuses to touch the network."""
    raise AssertionError(
        "A unit test attempted a real HTTP request. The session mock is not "
        "in place -- check that setUp() replaced client.session."
    )


class TrueNASClientTestCase(unittest.TestCase):
    """Base fixture: a client with a mocked session and no network access."""

    @classmethod
    def setUpClass(cls):
        guard = mock.patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            side_effect=_fail_on_real_request,
        )
        guard.start()
        cls.addClassCleanup(guard.stop)

    def setUp(self):
        self.client = TrueNASClient(
            host="truenas.example.com",
            username="admin",
            password="password123",
        )
        # Replace the *instance* attribute. Patching
        # `api_client.requests.Session` at method level does not work here:
        # unittest runs setUp() before the @patch decorator activates, so the
        # client keeps a real Session and the mock is never consulted. That
        # was the original defect behind this test module's failures.
        self.session = mock.MagicMock()
        self.client.session = self.session

    def _set_response(self, payload):
        """Point the mocked session at a canned JSON response."""
        response = mock.MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        self.session.request.return_value = response
        return response


class TestNetworkGuard(TrueNASClientTestCase):
    """The guard itself must work, or every other test here is worthless."""

    def test_real_request_is_blocked(self):
        client = TrueNASClient(
            host="truenas.example.com",
            username="admin",
            password="password123",
        )
        # Deliberately NOT mocked -- this client holds a real Session.
        with self.assertRaises(AssertionError) as ctx:
            client.is_eula_accepted()
        self.assertIn("real HTTP request", str(ctx.exception))


class TestClientConstruction(TrueNASClientTestCase):
    """Wiring performed in __init__."""

    def test_base_url_composed_from_host_and_port(self):
        client = TrueNASClient(
            host="nas.internal",
            username="u",
            password="p",
            port=8443,
        )
        self.assertEqual(
            client.base_url, "https://nas.internal:8443/api/v2.0"
        )

    def test_credentials_and_ssl_applied_to_session(self):
        client = TrueNASClient(
            host="nas.internal",
            username="svc",
            password="secret",
            verify_ssl=True,
        )
        self.assertEqual(client.session.auth, ("svc", "secret"))
        self.assertTrue(client.session.verify)


class TestEulaCheck(TrueNASClientTestCase):
    """EULA status reporting."""

    def test_returns_true_when_accepted(self):
        self._set_response({"accepted": True})

        result = self.client.is_eula_accepted()

        self.assertTrue(result)
        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/truenas/is_eula_accepted"
        )

    def test_returns_false_when_not_accepted(self):
        self._set_response({"accepted": False})

        self.assertFalse(self.client.is_eula_accepted())

    def test_returns_false_when_key_absent(self):
        self._set_response({})

        self.assertFalse(self.client.is_eula_accepted())


class TestPoolOperations(TrueNASClientTestCase):
    """Storage pool queries."""

    def test_get_pool_list_returns_payload(self):
        self._set_response([
            {"name": "tank", "size": 1073741824},
            {"name": "data", "size": 2147483648},
        ])

        result = self.client.get_pool_list()

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/pool"
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "tank")


class TestDatasetIdEncoding(TrueNASClientTestCase):
    """Dataset IDs are percent-encoded ZFS paths, not URL path segments."""

    def test_simple_name_encodes_separator(self):
        self.assertEqual(
            TrueNASClient._dataset_id("tank", "vol1"), "tank%2Fvol1"
        )

    def test_nested_name_encodes_every_separator(self):
        # Proxmox-created zvols live under nested datasets, so this is the
        # shape manage_existing (#20) will actually be handed.
        self.assertEqual(
            TrueNASClient._dataset_id("tank", "proxmox/vm-100-disk-0"),
            "tank%2Fproxmox%2Fvm-100-disk-0",
        )

    def test_no_raw_slash_survives_encoding(self):
        encoded = TrueNASClient._dataset_id("tank", "a/b/c")
        self.assertNotIn("/", encoded)


class TestZvolOperations(TrueNASClientTestCase):
    """Zvol lifecycle against /pool/dataset."""

    def test_create_zvol_posts_expected_payload(self):
        self._set_response({"id": "tank/volume1", "name": "tank/volume1"})

        result = self.client.create_zvol(
            pool="tank",
            name="volume1",
            size_gb=1,
        )

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/pool/dataset",
            json={
                "name": "tank/volume1",
                "type": "VOLUME",
                "volsize": 1073741824,
                "volmode": "GEOM",
                "sparse": True,
            },
        )
        self.assertEqual(result["name"], "tank/volume1")

    def test_create_zvol_converts_gb_to_bytes(self):
        self._set_response({})

        self.client.create_zvol(pool="tank", name="v", size_gb=10)

        payload = self.session.request.call_args.kwargs["json"]
        self.assertEqual(payload["volsize"], 10 * 1024 ** 3)

    def test_create_zvol_honours_sparse_false(self):
        self._set_response({})

        self.client.create_zvol(
            pool="tank", name="v", size_gb=1, sparse=False
        )

        payload = self.session.request.call_args.kwargs["json"]
        self.assertFalse(payload["sparse"])

    def test_create_zvol_merges_extra_properties(self):
        self._set_response({})

        self.client.create_zvol(
            pool="tank", name="v", size_gb=1, comments="managed by cinder"
        )

        payload = self.session.request.call_args.kwargs["json"]
        self.assertEqual(payload["comments"], "managed by cinder")
        self.assertEqual(payload["type"], "VOLUME")

    def test_get_zvol_uses_encoded_id(self):
        self._set_response({"name": "tank/volume1"})

        result = self.client.get_zvol("tank", "volume1")

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/pool/dataset/id/tank%2Fvolume1"
        )
        self.assertEqual(result["name"], "tank/volume1")

    def test_delete_zvol_sends_recursive_flag(self):
        self._set_response({})

        self.client.delete_zvol("tank", "volume1")

        self.session.request.assert_called_once_with(
            "DELETE",
            f"{BASE_URL}/pool/dataset/id/tank%2Fvolume1",
            json={"recursive": False},
        )

    def test_delete_zvol_recursive_true(self):
        self._set_response({})

        self.client.delete_zvol("tank", "volume1", recursive=True)

        payload = self.session.request.call_args.kwargs["json"]
        self.assertTrue(payload["recursive"])

    def test_resize_zvol_puts_new_volsize(self):
        self._set_response({})

        self.client.resize_zvol("tank", "volume1", new_size_gb=20)

        self.session.request.assert_called_once_with(
            "PUT",
            f"{BASE_URL}/pool/dataset/id/tank%2Fvolume1",
            json={"volsize": 20 * 1024 ** 3},
        )

    def test_list_zvols_filters_by_type_and_pool(self):
        self._set_response([{"name": "tank/v1"}])

        result = self.client.list_zvols("tank")

        self.session.request.assert_called_once_with(
            "GET",
            f"{BASE_URL}/pool/dataset",
            params={"type": "VOLUME", "name__startswith": "tank/"},
        )
        self.assertEqual(len(result), 1)

    def test_nested_zvol_round_trips_through_delete(self):
        self._set_response({})

        self.client.delete_zvol("tank", "proxmox/vm-100-disk-0")

        self.session.request.assert_called_once_with(
            "DELETE",
            f"{BASE_URL}/pool/dataset/id/"
            "tank%2Fproxmox%2Fvm-100-disk-0",
            json={"recursive": False},
        )


class TestIscsiOperations(TrueNASClientTestCase):
    """iSCSI target and target-extent request shapes."""

    def test_create_iscsi_target_posts_expected_payload(self):
        self._set_response({
            "id": 1,
            "name": "iqn.2005-10.org.freenas.ctl:volume1",
        })

        result = self.client.create_iscsi_target(
            name="iqn.2005-10.org.freenas.ctl:volume1"
        )

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/target",
            json={
                "name": "iqn.2005-10.org.freenas.ctl:volume1",
                "alias": None,
            },
        )
        self.assertEqual(result["id"], 1)

    def test_create_iscsi_target_extent_posts_ids(self):
        self._set_response({"id": 7})

        self.client.create_iscsi_target_extent(
            target_id=1, extent_id=2, lun_id=3
        )

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/targetextent",
            json={"target": 1, "extent": 2, "lunid": 3},
        )


class TestErrorPropagation(TrueNASClientTestCase):
    """HTTP failures must reach the caller unswallowed."""

    def test_http_error_raised_and_json_not_parsed(self):
        response = self._set_response({})
        response.raise_for_status.side_effect = requests.HTTPError("401")

        with self.assertRaises(requests.HTTPError):
            self.client.get_pool_list()

        # raise_for_status() must run *before* json(), so a failed request
        # never has its body parsed.
        response.json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
