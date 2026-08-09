"""
Unit tests for the TrueNAS API client.

Every test here runs fully offline. `TrueNASAPIClientTestCase` installs a
network guard that turns any real HTTP request into an explicit assertion
failure, so a mock that has stopped working surfaces immediately instead of
hanging on DNS or silently depending on runner network policy.

These tests pin the client's *current* behaviour. Zvol operations moved to
``/pool/dataset`` in #9, auth moved to a Bearer API key in #10, and error
handling gained typed exceptions, timeouts and retry in #11. The zvol, auth
and error-mapping paths have been checked against a real appliance
(TrueNAS-25.10.5) in #35 and #11; the status codes and 422 bodies asserted
below are transcribed from that appliance, not invented.

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

import json
import logging
import unittest
from unittest import mock

import requests

from truenas_cinder_driver import api_client
from truenas_cinder_driver.api_client import (
    DEFAULT_TIMEOUT,
    MAX_RETRY_AFTER,
    TrueNASAPIAuthError,
    TrueNASAPIClient,
    TrueNASAPIConnectionError,
    TrueNASAPIError,
    TrueNASAPINotFoundError,
    TrueNASAPITimeoutError,
)


BASE_URL = "https://truenas.example.com/api/v2.0"
API_KEY = "test-api-key"


def _fail_on_real_request(*args, **kwargs):
    """Transport adapter stand-in that refuses to touch the network."""
    raise AssertionError(
        "A unit test attempted a real HTTP request. The session mock is not "
        "in place -- check that setUp() replaced client.session."
    )


class TrueNASAPIClientTestCase(unittest.TestCase):
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

        # The retry and rollback paths log by design. Without a handler,
        # logging's lastResort dumps them to stderr and buries the test
        # results. assertLogs still works -- it installs its own handler.
        logger = logging.getLogger(api_client.__name__)
        handler = logging.NullHandler()
        logger.addHandler(handler)
        cls.addClassCleanup(logger.removeHandler, handler)

    def setUp(self):
        self.client = TrueNASAPIClient(
            base_url="https://truenas.example.com",
            api_key=API_KEY,
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

    @staticmethod
    def _error_response(status_code, body=None, text=None, headers=None):
        """Build a failing response the way the real appliance shapes them.

        `body` is the decoded JSON error body; `text` overrides the rendered
        text, which otherwise mirrors it.
        """
        response = mock.MagicMock()
        response.status_code = status_code
        response.headers = headers or {}
        response.json.return_value = body
        if text is None:
            text = "" if body is None else json.dumps(body)
        response.text = text
        response.content = text.encode()
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} Error"
        )
        return response

    def _set_error(self, status_code, body=None, text=None, headers=None):
        """Point the mocked session at a single failing response."""
        response = self._error_response(status_code, body, text, headers)
        self.session.request.return_value = response
        return response


class TestNetworkGuard(TrueNASAPIClientTestCase):
    """The guard itself must work, or every other test here is worthless."""

    def test_real_request_is_blocked(self):
        client = TrueNASAPIClient(
            base_url="https://truenas.example.com",
            api_key=API_KEY,
        )
        # Deliberately NOT mocked -- this client holds a real Session.
        with self.assertRaises(AssertionError) as ctx:
            client.is_eula_accepted()
        self.assertIn("real HTTP request", str(ctx.exception))


class TestClientConstruction(TrueNASAPIClientTestCase):
    """Wiring performed in __init__."""

    def test_bearer_token_set_on_session(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key="sekrit"
        )
        self.assertEqual(
            client.session.headers["Authorization"], "Bearer sekrit"
        )
        self.assertEqual(
            client.session.headers["Content-Type"], "application/json"
        )

    def test_basic_auth_is_not_used(self):
        # Basic auth was the pre-#10 scheme. If it ever comes back, an
        # interactive admin password re-enters cinder.conf.
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key="sekrit"
        )
        self.assertIsNone(client.session.auth)

    def test_ssl_verification_defaults_on(self):
        # Defaulted to False before #10, silently disabling certificate
        # checks for every deployment that did not override it.
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key=API_KEY
        )
        self.assertTrue(client.session.verify)

    def test_ssl_verification_can_be_disabled(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal",
            api_key=API_KEY,
            verify_ssl=False,
        )
        self.assertFalse(client.session.verify)

    def test_base_url_accepts_bare_host(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key=API_KEY
        )
        self.assertEqual(client.base_url, "https://nas.internal")

    def test_base_url_strips_trailing_slash(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal/", api_key=API_KEY
        )
        self.assertEqual(client.base_url, "https://nas.internal")

    def test_base_url_tolerates_api_prefix(self):
        # cinder.conf's truenas_api_url is documented with the /api/v2.0
        # suffix, so accepting both forms avoids a doubled path.
        client = TrueNASAPIClient(
            base_url="https://nas.internal/api/v2.0", api_key=API_KEY
        )
        self.assertEqual(client.base_url, "https://nas.internal")

    def test_non_default_port_preserved(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal:8443", api_key=API_KEY
        )
        self.assertEqual(client.base_url, "https://nas.internal:8443")


class TestRequestUrlComposition(TrueNASAPIClientTestCase):
    """The API prefix is applied once, wherever base_url came from."""

    def test_prefix_applied_to_endpoint(self):
        self._set_response({})

        self.client.get_pool_list()

        self.session.request.assert_called_once_with(
            "GET", "https://truenas.example.com/api/v2.0/pool",
            timeout=DEFAULT_TIMEOUT,
        )

    def test_prefix_not_doubled_when_base_url_includes_it(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal/api/v2.0", api_key=API_KEY
        )
        session = mock.MagicMock()
        response = mock.MagicMock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        session.request.return_value = response
        client.session = session

        client.get_pool_list()

        session.request.assert_called_once_with(
            "GET", "https://nas.internal/api/v2.0/pool",
            timeout=DEFAULT_TIMEOUT,
        )


class TestEmptyResponseBody(TrueNASAPIClientTestCase):
    """DELETE and other 204s carry no body; json() would raise on them."""

    def test_delete_zvol_does_not_raise_on_empty_body(self):
        # delete_zvol returns None regardless -- what matters is that
        # json() is never reached on a bodyless response.
        response = mock.MagicMock()
        response.content = b""
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("no json")
        self.session.request.return_value = response

        result = self.client.delete_zvol("tank", "volume1")

        self.assertIsNone(result)
        response.json.assert_not_called()

    def test_empty_body_returns_empty_dict(self):
        response = mock.MagicMock()
        response.content = b""
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("no json")
        self.session.request.return_value = response

        self.assertEqual(self.client.get_pool_list(), {})

    def test_non_empty_body_is_parsed(self):
        response = mock.MagicMock()
        response.content = b'{"name": "tank"}'
        response.raise_for_status.return_value = None
        response.json.return_value = {"name": "tank"}
        self.session.request.return_value = response

        self.assertEqual(self.client.get_zvol("tank", "v"), {"name": "tank"})


class TestCredentialHandling(TrueNASAPIClientTestCase):
    """Regression guards against the API key leaking into logged output.

    These are guards, not proof of redaction: there is no redaction
    mechanism to test. `object.__repr__` never includes instance
    attributes, so the repr assertion passes trivially today -- its value
    is failing loudly if someone later adds a `__repr__` that interpolates
    the session or the key.

    The key *is* present in `session.headers["Authorization"]` by design,
    so anything that dumps the session wholesale will still expose it.

    The exception-message guards below are stronger than the repr ones,
    because #11 gave errors real content to leak: a status, a request
    context, and a slice of the response body.
    """

    def test_api_key_absent_from_repr(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key="super-secret-key"
        )
        self.assertNotIn("super-secret-key", repr(client))

    def test_api_key_absent_from_base_url(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key="super-secret-key"
        )
        self.assertNotIn("super-secret-key", client.base_url)

    def _failing_client(self, base_url, api_key, status=401):
        client = TrueNASAPIClient(base_url=base_url, api_key=api_key)
        client.session = mock.MagicMock()
        client.session.request.return_value = self._error_response(
            status, text="Invalid API key"
        )
        return client

    def test_api_key_absent_from_exception_message(self):
        client = self._failing_client(
            "https://nas.internal", "super-secret-key"
        )

        with self.assertRaises(TrueNASAPIError) as ctx:
            client.get_pool_list()

        self.assertNotIn("super-secret-key", str(ctx.exception))

    def test_inline_url_credentials_absent_from_exception_message(self):
        # Why error messages carry `endpoint` and not the full URL. A
        # base_url with inline credentials is legal and requests honours
        # it, so interpolating the URL would put a password into a message
        # that Cinder will log.
        client = self._failing_client(
            "https://admin:hunter2@nas.internal", API_KEY
        )

        with self.assertRaises(TrueNASAPIError) as ctx:
            client.get_pool_list()

        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertIn("/pool", str(ctx.exception))

    def test_inline_url_credentials_absent_from_timeout_message(self):
        client = TrueNASAPIClient(
            base_url="https://admin:hunter2@nas.internal", api_key=API_KEY
        )
        client.session = mock.MagicMock()
        client.session.request.side_effect = requests.ReadTimeout("slow")

        with self.assertRaises(TrueNASAPIError) as ctx:
            client.get_pool_list()

        self.assertNotIn("hunter2", str(ctx.exception))


class TestEulaCheck(TrueNASAPIClientTestCase):
    """EULA status reporting.

    The endpoint returns a bare JSON boolean, not an object. The client
    previously did ``data.get("accepted", False)``, which raised
    ``AttributeError: 'bool' object has no attribute 'get'`` on the very
    first call `do_setup()` makes. Verified against TrueNAS-25.10.5 in #35.
    """

    def test_returns_true_for_bare_true(self):
        self._set_response(True)

        result = self.client.is_eula_accepted()

        self.assertTrue(result)
        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/truenas/is_eula_accepted",
            timeout=DEFAULT_TIMEOUT,
        )

    def test_returns_false_for_bare_false(self):
        self._set_response(False)

        self.assertFalse(self.client.is_eula_accepted())

    def test_bare_boolean_does_not_raise(self):
        # Regression guard for the original defect: any dict-style access
        # on the response would raise here.
        self._set_response(False)

        try:
            self.client.is_eula_accepted()
        except AttributeError as exc:
            self.fail(f"treated a bare boolean as an object: {exc}")


class TestPoolOperations(TrueNASAPIClientTestCase):
    """Storage pool queries."""

    def test_get_pool_list_returns_payload(self):
        self._set_response([
            {"name": "tank", "size": 1073741824},
            {"name": "data", "size": 2147483648},
        ])

        result = self.client.get_pool_list()

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/pool", timeout=DEFAULT_TIMEOUT
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "tank")


class TestDatasetIdEncoding(TrueNASAPIClientTestCase):
    """Dataset IDs are percent-encoded ZFS paths, not URL path segments."""

    def test_simple_name_encodes_separator(self):
        self.assertEqual(
            TrueNASAPIClient._dataset_id("tank", "vol1"), "tank%2Fvol1"
        )

    def test_nested_name_encodes_every_separator(self):
        # Proxmox-created zvols live under nested datasets, so this is the
        # shape manage_existing (#20) will actually be handed.
        self.assertEqual(
            TrueNASAPIClient._dataset_id("tank", "proxmox/vm-100-disk-0"),
            "tank%2Fproxmox%2Fvm-100-disk-0",
        )

    def test_no_raw_slash_survives_encoding(self):
        encoded = TrueNASAPIClient._dataset_id("tank", "a/b/c")
        self.assertNotIn("/", encoded)


class TestZvolOperations(TrueNASAPIClientTestCase):
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
                "sparse": True,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(result["name"], "tank/volume1")

    def test_create_zvol_omits_volmode(self):
        # volmode is FreeBSD terminology and Scale rejects it. An
        # unrecognised key breaks VOLUME schema discrimination, producing a
        # 422 that misleadingly points at `type`. Verified in #35.
        self._set_response({})

        self.client.create_zvol(pool="tank", name="v", size_gb=1)

        payload = self.session.request.call_args.kwargs["json"]
        self.assertNotIn("volmode", payload)

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
            "GET", f"{BASE_URL}/pool/dataset/id/tank%2Fvolume1",
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(result["name"], "tank/volume1")

    def test_delete_zvol_sends_recursive_flag(self):
        self._set_response({})

        self.client.delete_zvol("tank", "volume1")

        self.session.request.assert_called_once_with(
            "DELETE",
            f"{BASE_URL}/pool/dataset/id/tank%2Fvolume1",
            json={"recursive": False},
            timeout=DEFAULT_TIMEOUT,
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
            timeout=DEFAULT_TIMEOUT,
        )

    def test_list_zvols_filters_by_type_and_pool(self):
        self._set_response([{"name": "tank/v1"}])

        result = self.client.list_zvols("tank")

        self.session.request.assert_called_once_with(
            "GET",
            f"{BASE_URL}/pool/dataset",
            params={"type": "VOLUME", "name__^": "tank/"},
            timeout=DEFAULT_TIMEOUT,
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
            timeout=DEFAULT_TIMEOUT,
        )


class TestIscsiOperations(TrueNASAPIClientTestCase):
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
            timeout=DEFAULT_TIMEOUT,
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
            timeout=DEFAULT_TIMEOUT,
        )


# Error bodies below are transcribed verbatim from TrueNAS-25.10.5. The field
# key varies by operation, which is why _is_enoent scans the whole body.
ENOENT_DELETE_BODY = {
    "null": [
        {
            "message": "PoolDataset Dev-Pool/gone does not exist",
            "errno": 2,
        }
    ]
}
ENOENT_PUT_BODY = {
    "id": [{"message": "Dev-Pool/gone does not exist", "errno": 2}]
}
# errno 22, but the message *also* says "does not exist" -- the reason the
# mapping keys off errno rather than the message text.
BAD_POOL_BODY = {
    "pool_dataset_create.name": [
        {"message": "zpool (NoSuchPool123) does not exist.", "errno": 22}
    ]
}


class TestErrorPropagation(TrueNASAPIClientTestCase):
    """HTTP failures must reach the caller as typed exceptions."""

    def test_http_error_becomes_typed_error(self):
        response = self._set_error(500, text="boom")

        with self.assertRaises(TrueNASAPIError):
            self.client.get_pool_list()

        # The status check must run before the body is parsed as a result,
        # so a failed request never returns half-decoded success data.
        response.json.assert_not_called()

    def test_raw_requests_exception_does_not_escape(self):
        # The whole point of the hierarchy: #14 translates to
        # VolumeBackendAPIException with one except clause.
        self._set_error(500, text="boom")

        with self.assertRaises(TrueNASAPIError):
            try:
                self.client.get_pool_list()
            except requests.RequestException as exc:
                self.fail(f"raw requests exception escaped: {exc!r}")

    def test_error_carries_request_context(self):
        self._set_error(500, text="boom")

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.get_pool_list()

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.method, "GET")
        self.assertEqual(ctx.exception.endpoint, "/pool")

    def test_error_chains_the_original(self):
        self._set_error(500, text="boom")

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.get_pool_list()

        self.assertIsInstance(ctx.exception.__cause__, requests.HTTPError)

    def test_body_included_in_message(self):
        self._set_error(500, text="pool is degraded")

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.get_pool_list()

        self.assertIn("pool is degraded", str(ctx.exception))

    def test_long_body_is_truncated(self):
        self._set_error(500, text="x" * 5000)

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.get_pool_list()

        self.assertLess(len(str(ctx.exception)), 800)

    def test_unreadable_body_does_not_mask_the_error(self):
        # Building the message must never raise over the top of the failure
        # it is reporting.
        response = self._set_error(500)
        type(response).text = mock.PropertyMock(
            side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
        )

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.get_pool_list()

        self.assertIn("HTTP 500", str(ctx.exception))


class TestNotFoundMapping(TrueNASAPIClientTestCase):
    """404 and ENOENT-flavoured 422 both mean "already gone".

    Verified against TrueNAS-25.10.5 in #11: GET on a missing dataset
    answers 404, but DELETE and PUT answer 422 with errno 2. DELETE is the
    operation idempotent deletes depend on, so a 404-only mapping would
    never have fired where it mattered.
    """

    def test_404_maps_to_not_found(self):
        self._set_error(404, body={"message": ""})

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.get_zvol("tank", "gone")

    def test_422_enoent_on_delete_maps_to_not_found(self):
        self._set_error(422, body=ENOENT_DELETE_BODY)

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.delete_zvol("tank", "gone")

    def test_422_enoent_on_resize_maps_to_not_found(self):
        self._set_error(422, body=ENOENT_PUT_BODY)

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.resize_zvol("tank", "gone", new_size_gb=2)

    def test_422_validation_error_is_not_not_found(self):
        # The regression this guards: the message says "does not exist" but
        # errno is 22. Reading it as NotFound would let the driver report a
        # failed create against a misconfigured pool as a successful delete.
        self._set_error(422, body=BAD_POOL_BODY)

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.create_zvol(pool="NoSuchPool123", name="v", size_gb=1)

        self.assertNotIsInstance(ctx.exception, TrueNASAPINotFoundError)

    def test_mixed_errnos_are_not_not_found(self):
        # Conservative direction: only an unambiguous ENOENT counts.
        self._set_error(422, body={
            "a": [{"message": "gone", "errno": 2}],
            "b": [{"message": "invalid", "errno": 22}],
        })

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.delete_zvol("tank", "v")

        self.assertNotIsInstance(ctx.exception, TrueNASAPINotFoundError)

    def test_422_with_unparseable_body_is_not_not_found(self):
        response = self._set_error(422, text="<html>gateway</html>")
        response.json.side_effect = ValueError("not json")

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.delete_zvol("tank", "v")

        self.assertNotIsInstance(ctx.exception, TrueNASAPINotFoundError)

    def test_422_with_non_dict_body_is_not_not_found(self):
        self._set_error(422, body=["unexpected"])

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.delete_zvol("tank", "v")

        self.assertNotIsInstance(ctx.exception, TrueNASAPINotFoundError)

    def test_422_with_no_error_entries_is_not_not_found(self):
        # `all([])` is True, so a body carrying no errno at all would read
        # as ENOENT without the explicit emptiness guard. The appliance
        # does return bare {"message": ...} bodies on some paths, so this
        # is reachable, and it fails in the dangerous direction: Cinder
        # would record a volume as deleted that still exists.
        self._set_error(422, body={"message": "something else entirely"})

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.delete_zvol("tank", "v")

        self.assertNotIsInstance(ctx.exception, TrueNASAPINotFoundError)

    def test_422_with_empty_error_list_is_not_not_found(self):
        self._set_error(422, body={"null": []})

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.delete_zvol("tank", "v")

        self.assertNotIsInstance(ctx.exception, TrueNASAPINotFoundError)

    def test_not_found_is_a_truenas_error(self):
        self._set_error(404, body={"message": ""})

        with self.assertRaises(TrueNASAPIError):
            self.client.get_zvol("tank", "gone")


class TestAuthMapping(TrueNASAPIClientTestCase):
    """401/403 get their own type so a bad key is actionable."""

    def test_401_maps_to_auth_error(self):
        # Real body from the appliance for a revoked key.
        self._set_error(401, text="Invalid API key")

        with self.assertRaises(TrueNASAPIAuthError):
            self.client.get_pool_list()

    def test_403_maps_to_auth_error(self):
        self._set_error(403, text="Forbidden")

        with self.assertRaises(TrueNASAPIAuthError):
            self.client.get_pool_list()

    def test_auth_error_names_the_config_option(self):
        self._set_error(401, text="Invalid API key")

        with self.assertRaises(TrueNASAPIAuthError) as ctx:
            self.client.get_pool_list()

        self.assertIn("truenas_api_key", str(ctx.exception))

    def test_auth_error_is_a_truenas_error(self):
        self._set_error(401, text="Invalid API key")

        with self.assertRaises(TrueNASAPIError):
            self.client.get_pool_list()


class TestTimeouts(TrueNASAPIClientTestCase):
    """Every request is bounded; an unbounded one wedges a cinder worker."""

    def test_default_timeout_applied(self):
        self._set_response({})

        self.client.get_pool_list()

        self.assertEqual(
            self.session.request.call_args.kwargs["timeout"], DEFAULT_TIMEOUT
        )

    def test_timeout_is_configurable(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key=API_KEY, timeout=(1, 2)
        )
        response = mock.MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = []
        client.session = mock.MagicMock()
        client.session.request.return_value = response

        client.get_pool_list()

        self.assertEqual(
            client.session.request.call_args.kwargs["timeout"], (1, 2)
        )

    def test_caller_may_override_per_request(self):
        self._set_response({})

        self.client._make_request("GET", "/pool", timeout=99)

        self.assertEqual(
            self.session.request.call_args.kwargs["timeout"], 99
        )

    def test_timeout_becomes_typed_error(self):
        self.session.request.side_effect = requests.ConnectTimeout("slow")

        with self.assertRaises(TrueNASAPITimeoutError):
            self.client.get_pool_list()

    def test_timeout_error_is_a_connection_error(self):
        # The driver should be able to treat "may or may not have landed"
        # as one category.
        self.session.request.side_effect = requests.ReadTimeout("slow")

        with self.assertRaises(TrueNASAPIConnectionError):
            self.client.get_pool_list()

    def test_timeout_message_warns_it_may_still_be_running(self):
        self.session.request.side_effect = requests.ReadTimeout("slow")

        with self.assertRaises(TrueNASAPITimeoutError) as ctx:
            self.client.delete_zvol("tank", "v")

        self.assertIn("may still be processing", str(ctx.exception))

    def test_connection_error_becomes_typed_error(self):
        self.session.request.side_effect = requests.ConnectionError("refused")

        with self.assertRaises(TrueNASAPIConnectionError) as ctx:
            self.client.get_pool_list()

        self.assertNotIsInstance(ctx.exception, TrueNASAPITimeoutError)

    def test_timeout_is_not_retried(self):
        # A read timeout may mean the appliance is still working on the
        # request; replaying a create or delete on top of that is worse than
        # failing. Retrying timeouts safely needs idempotency awareness --
        # see #12.
        self.session.request.side_effect = requests.ReadTimeout("slow")

        with self.assertRaises(TrueNASAPITimeoutError):
            self.client.create_zvol(pool="tank", name="v", size_gb=1)

        self.assertEqual(self.session.request.call_count, 1)


class TestRetry(TrueNASAPIClientTestCase):
    """429 and 503 are retried; nothing else is."""

    def setUp(self):
        super().setUp()
        sleep = mock.patch.object(api_client.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def _ok_response(self, payload):
        response = mock.MagicMock()
        response.status_code = 200
        response.content = b'{"ok": true}'
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_retries_503_then_succeeds(self):
        self.session.request.side_effect = [
            self._error_response(503, text="starting up"),
            self._ok_response([{"name": "tank"}]),
        ]

        result = self.client.get_pool_list()

        self.assertEqual(result, [{"name": "tank"}])
        self.assertEqual(self.session.request.call_count, 2)

    def test_retries_429_then_succeeds(self):
        self.session.request.side_effect = [
            self._error_response(429, text="slow down"),
            self._ok_response([]),
        ]

        self.assertEqual(self.client.get_pool_list(), [])
        self.assertEqual(self.session.request.call_count, 2)

    def test_retry_exhausted_raises_last_status(self):
        self.session.request.side_effect = [
            self._error_response(503, text="down") for _ in range(3)
        ]

        with self.assertRaises(TrueNASAPIError) as ctx:
            self.client.get_pool_list()

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(self.session.request.call_count, 3)

    def test_400_is_not_retried(self):
        self._set_error(400, text="bad request")

        with self.assertRaises(TrueNASAPIError):
            self.client.get_pool_list()

        self.assertEqual(self.session.request.call_count, 1)

    def test_404_is_not_retried(self):
        self._set_error(404, body={"message": ""})

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.get_zvol("tank", "gone")

        self.assertEqual(self.session.request.call_count, 1)

    def test_500_is_not_retried(self):
        # A 500 may have applied a partial change, unlike 429/503.
        self._set_error(500, text="internal error")

        with self.assertRaises(TrueNASAPIError):
            self.client.get_pool_list()

        self.assertEqual(self.session.request.call_count, 1)

    def test_backoff_is_exponential(self):
        self.session.request.side_effect = [
            self._error_response(503, text="down") for _ in range(3)
        ]

        with self.assertRaises(TrueNASAPIError):
            self.client.get_pool_list()

        self.assertEqual(
            [call.args[0] for call in self.sleep.call_args_list], [0.5, 1.0]
        )

    def test_backoff_factor_is_configurable(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal",
            api_key=API_KEY,
            backoff_factor=3.0,
        )
        client.session = mock.MagicMock()
        client.session.request.side_effect = [
            self._error_response(503, text="down"),
            self._ok_response([]),
        ]

        client.get_pool_list()

        self.sleep.assert_called_once_with(3.0)

    def test_retry_after_header_is_honoured(self):
        self.session.request.side_effect = [
            self._error_response(
                429, text="wait", headers={"Retry-After": "7"}
            ),
            self._ok_response([]),
        ]

        self.client.get_pool_list()

        self.sleep.assert_called_once_with(7.0)

    def test_retry_after_is_clamped_not_discarded(self):
        # Clamped rather than ignored: retrying in 0.5s after the appliance
        # asked for an hour is how a rate limit gets worse.
        self.session.request.side_effect = [
            self._error_response(
                429, text="wait", headers={"Retry-After": "3600"}
            ),
            self._ok_response([]),
        ]

        self.client.get_pool_list()

        self.sleep.assert_called_once_with(MAX_RETRY_AFTER)

    def test_http_date_retry_after_falls_back_to_backoff(self):
        self.session.request.side_effect = [
            self._error_response(
                503,
                text="wait",
                headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            ),
            self._ok_response([]),
        ]

        self.client.get_pool_list()

        self.sleep.assert_called_once_with(0.5)

    def test_negative_retry_after_falls_back_to_backoff(self):
        self.session.request.side_effect = [
            self._error_response(
                503, text="wait", headers={"Retry-After": "-5"}
            ),
            self._ok_response([]),
        ]

        self.client.get_pool_list()

        self.sleep.assert_called_once_with(0.5)

    def test_max_attempts_one_disables_retry(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key=API_KEY, max_attempts=1
        )
        client.session = mock.MagicMock()
        client.session.request.return_value = self._error_response(
            503, text="down"
        )

        with self.assertRaises(TrueNASAPIError):
            client.get_pool_list()

        self.assertEqual(client.session.request.call_count, 1)
        self.sleep.assert_not_called()

    def test_max_attempts_is_configurable(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key=API_KEY, max_attempts=5
        )
        client.session = mock.MagicMock()
        client.session.request.side_effect = [
            self._error_response(503, text="down") for _ in range(5)
        ]

        with self.assertRaises(TrueNASAPIError):
            client.get_pool_list()

        self.assertEqual(client.session.request.call_count, 5)

    def test_zero_max_attempts_rejected(self):
        # Silently doing nothing would be far worse than failing loudly.
        with self.assertRaises(ValueError):
            TrueNASAPIClient(
                base_url="https://nas.internal",
                api_key=API_KEY,
                max_attempts=0,
            )

    def test_retry_is_logged(self):
        # Silent retries hide a struggling appliance behind merely slow
        # volume operations.
        self.session.request.side_effect = [
            self._error_response(503, text="down"),
            self._ok_response([]),
        ]

        with self.assertLogs(api_client.__name__, level="WARNING") as logs:
            self.client.get_pool_list()

        self.assertIn("retrying", logs.output[0])
        self.assertIn("503", logs.output[0])

    def test_retry_replays_the_same_payload(self):
        self.session.request.side_effect = [
            self._error_response(503, text="down"),
            self._ok_response({}),
        ]

        self.client.create_zvol(pool="tank", name="v", size_gb=1)

        first, second = self.session.request.call_args_list
        self.assertEqual(first.kwargs["json"], second.kwargs["json"])


class TestBestEffortDelete(TrueNASAPIClientTestCase):
    """Rollback cleanup must never mask the error that triggered it."""

    def test_returns_true_on_success(self):
        delete = mock.MagicMock()

        self.assertTrue(
            self.client.best_effort_delete(delete, 7, what="extent 7")
        )
        delete.assert_called_once_with(7)

    def test_already_gone_counts_as_success(self):
        delete = mock.MagicMock(
            side_effect=TrueNASAPINotFoundError("gone")
        )

        self.assertTrue(self.client.best_effort_delete(delete, 7))

    def test_failure_returns_false_without_raising(self):
        delete = mock.MagicMock(side_effect=TrueNASAPIError("still busy"))

        self.assertFalse(self.client.best_effort_delete(delete, 7))

    def test_unexpected_exception_is_swallowed(self):
        # Anything at all -- this runs inside an except block that is about
        # to re-raise something more useful.
        delete = mock.MagicMock(side_effect=RuntimeError("boom"))

        self.assertFalse(self.client.best_effort_delete(delete, 7))

    def test_original_error_survives_a_failed_rollback(self):
        # The scenario the helper exists for, end to end.
        delete = mock.MagicMock(side_effect=RuntimeError("cleanup broke"))

        with self.assertRaises(TrueNASAPIError) as ctx:
            try:
                raise TrueNASAPIError("target create failed")
            except TrueNASAPIError:
                self.client.best_effort_delete(delete, 7, what="extent 7")
                raise

        self.assertEqual(str(ctx.exception), "target create failed")

    def test_failure_is_logged_with_what_was_orphaned(self):
        # This log line is the only trace an operator gets that something
        # needs manual cleanup, so it has to name the object.
        delete = mock.MagicMock(side_effect=TrueNASAPIError("still busy"))

        with self.assertLogs(api_client.__name__, level="ERROR") as logs:
            self.client.best_effort_delete(delete, 7, what="iSCSI extent 7")

        self.assertIn("iSCSI extent 7", logs.output[0])
        self.assertIn("orphaned", logs.output[0])

    def test_keyword_arguments_are_forwarded(self):
        delete = mock.MagicMock()

        self.client.best_effort_delete(
            delete, "tank", "v", what="zvol", recursive=True
        )

        delete.assert_called_once_with("tank", "v", recursive=True)


if __name__ == "__main__":
    unittest.main()
