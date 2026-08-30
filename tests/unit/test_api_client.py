"""
Unit tests for the TrueNAS API client.

Every test here runs fully offline. `TrueNASAPIClientTestCase` installs a
network guard that turns any real HTTP request into an explicit assertion
failure, so a mock that has stopped working surfaces immediately instead of
hanging on DNS or silently depending on runner network policy.

These tests pin the client's *current* behaviour. Zvol operations moved to
``/pool/dataset`` in #9, auth moved to a Bearer API key in #10, error handling
gained typed exceptions, timeouts and retry in #11, and the iSCSI pipeline
landed in #12. The zvol, auth, error-mapping and iSCSI paths have all been
checked against a real appliance (TrueNAS-25.10.5) in #35, #11 and #12; the
status codes, 422 bodies and payload shapes asserted below are transcribed
from that appliance, not invented.

Several iSCSI tests assert what the client must *not* send -- title-case
``type``, a zvol in ``path``, ``auth_network``, ``delete_extents``. Those are
not hypothetical: each is a payload the appliance rejected or a flag that
would cause data loss, and a plain "does it work" test would not catch their
return.

The snapshot tests are the sharpest case of that. Before #42 those methods had
two stacked bugs -- the dead ``/zfs/snapshot`` path and an unencoded snapshot
id -- which both produced a 404, mapped to ``TrueNASAPINotFoundError``, and
were swallowed as "already deleted". The result reported success on every call
while deleting nothing. So the tests assert the *absence* of ``/zfs/`` and the
*presence* of percent-encoding, not merely that the happy path works.

Coverage is deliberately partial: clone, rollback and promote are untested
here because #13 and #21 add them along with their own test requirements.
"""

import importlib.metadata
import json
import logging
import traceback
import unittest
from unittest import mock

import requests

import truenas_cinder_driver
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

    def test_endpoint_not_url_in_message(self):
        client = self._failing_client("https://nas.internal", API_KEY)

        with self.assertRaises(TrueNASAPIError) as ctx:
            client.get_pool_list()

        self.assertIn("/pool", str(ctx.exception))
        self.assertNotIn("nas.internal", str(ctx.exception))


class TestCredentialsInExceptionChain(TrueNASAPIClientTestCase):
    """The typed exception is not the only thing that gets logged.

    ``raise ... from exc`` keeps the original ``requests.HTTPError`` as
    ``__cause__``, and *its* message is built by
    ``Response.raise_for_status()`` as ``"<status> <reason> for url:
    <response.url>"``. requests does not strip userinfo from that URL, so
    a ``base_url`` carrying inline credentials puts a password into
    anything that formats the chain -- ``LOG.exception``,
    ``traceback.format_exc``, Cinder's unhandled-exception logging.

    Asserting on ``str(typed_error)`` alone cannot see this, and a mocked
    ``HTTPError`` never builds the offending message in the first place.
    These tests therefore use real ``requests.Response`` objects and format
    the whole chain. Found in review of #11.
    """

    @staticmethod
    def _real_response(url, status=401, body=b"Invalid API key"):
        """A genuine requests.Response, not a mock.

        The leak lives in requests' own message construction, so a
        MagicMock cannot reproduce it.
        """
        response = requests.Response()
        response.status_code = status
        response.reason = "Unauthorized"
        response.url = url
        response._content = body
        return response

    def test_requests_really_does_leak_userinfo(self):
        # Pins the upstream behaviour the __init__ guard exists for. If
        # requests ever starts redacting, this fails and the guard can be
        # reconsidered rather than cargo-culted.
        response = self._real_response(
            "https://admin:hunter2@nas.internal/api/v2.0/pool"
        )

        with self.assertRaises(requests.HTTPError) as ctx:
            response.raise_for_status()

        self.assertIn("hunter2", str(ctx.exception))

    def test_inline_credentials_rejected_at_construction(self):
        with self.assertRaises(ValueError) as ctx:
            TrueNASAPIClient(
                base_url="https://admin:hunter2@nas.internal",
                api_key=API_KEY,
            )

        # The rejection itself must not echo what it rejected.
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_username_without_password_also_rejected(self):
        # Still overrides the Bearer header, so still broken.
        with self.assertRaises(ValueError):
            TrueNASAPIClient(
                base_url="https://admin@nas.internal", api_key=API_KEY
            )

    def test_inline_credentials_would_have_broken_bearer_auth(self):
        # The second reason for rejecting rather than sanitising: requests
        # replaces our Bearer header with Basic derived from the userinfo,
        # so the API key is silently discarded.
        session = requests.Session()
        session.headers.update({"Authorization": "Bearer my-api-key"})
        prepared = session.prepare_request(
            requests.Request("GET", "https://admin:hunter2@nas.internal/x")
        )

        self.assertNotIn("Bearer", prepared.headers["Authorization"])

    def test_full_exception_chain_carries_no_credentials(self):
        # End to end, formatted the way LOG.exception would format it.
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key="super-secret-key"
        )
        client.session = mock.MagicMock()
        client.session.request.return_value = self._real_response(
            "https://nas.internal/api/v2.0/pool"
        )

        with self.assertRaises(TrueNASAPIError) as ctx:
            client.get_pool_list()

        rendered = "".join(
            traceback.format_exception(
                type(ctx.exception), ctx.exception,
                ctx.exception.__traceback__,
            )
        )
        self.assertIn("__cause__", dir(ctx.exception))
        self.assertNotIn("super-secret-key", rendered)
        self.assertNotIn("hunter2", rendered)


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


VOLUME = "volume-4d9e1a5c-8f3b-4a21-9c77-2e6b0f1d3a84"
IQN_A = "iqn.2005-03.org.open-iscsi:nova-compute-01"
IQN_B = "iqn.2005-03.org.open-iscsi:nova-compute-02"


class IscsiTestCase(TrueNASAPIClientTestCase):
    """Adds a helper for probes that make more than one request."""

    def _set_responses(self, *payloads):
        """Queue one canned JSON response per subsequent request."""
        responses = []
        for payload in payloads:
            response = mock.MagicMock()
            response.json.return_value = payload
            response.raise_for_status.return_value = None
            responses.append(response)
        self.session.request.side_effect = responses
        return responses

    def _payload(self, call_index=0):
        """Return the JSON body of the nth request the client made."""
        return self.session.request.call_args_list[call_index].kwargs["json"]


class TestIscsiGlobalAndPortal(IscsiTestCase):
    """Global config and portal discovery/creation."""

    def test_zvol_disk_path_has_no_dev_prefix(self):
        # Verified against GET /iscsi/extent/disk_choices, which returned
        # exactly "zvol/Dev-Pool/<name>" as an acceptable `disk` value.
        self.assertEqual(
            self.client.zvol_disk_path("Dev-Pool", VOLUME),
            f"zvol/Dev-Pool/{VOLUME}",
        )

    def test_zvol_disk_path_rejects_the_shipped_dev_form(self):
        self.assertNotIn(
            "/dev/", self.client.zvol_disk_path("Dev-Pool", VOLUME)
        )

    def test_get_iscsi_global_config_reads_basename(self):
        self._set_response({
            "id": 1,
            "basename": "iqn.2005-10.org.freenas.ctl",
            "listen_port": 3260,
        })

        result = self.client.get_iscsi_global_config()

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/global", timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(result["basename"], "iqn.2005-10.org.freenas.ctl")

    def test_get_portals_returns_empty_list_on_clean_appliance(self):
        # The design spec assumed portals pre-exist. A fresh appliance has
        # none, so callers must cope with [].
        self._set_response([])

        self.assertEqual(self.client.get_portals(), [])

    def test_create_portal_defaults_to_all_addresses(self):
        self._set_response({"id": 1, "listen": [{"ip": "0.0.0.0",
                                                 "port": 3260}], "tag": 1})

        portal_id = self.client.create_portal()

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/portal",
            json={"listen": [{"ip": "0.0.0.0"}], "comment": ""},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(portal_id, 1)

    def test_create_portal_wraps_each_ip_in_an_object(self):
        # `listen` items are objects with an `ip` key, not bare strings.
        self._set_response({"id": 2})

        self.client.create_portal(listen_ips=["10.0.0.5", "10.0.0.6"])

        self.assertEqual(
            self._payload()["listen"],
            [{"ip": "10.0.0.5"}, {"ip": "10.0.0.6"}],
        )

    def test_create_portal_does_not_send_a_port(self):
        # `port` is response-only; it comes from the global listen_port.
        self._set_response({"id": 3})

        self.client.create_portal()

        for item in self._payload()["listen"]:
            self.assertNotIn("port", item)


class TestInitiatorGroups(IscsiTestCase):
    """get_or_create_initiator_group deduplication."""

    def test_reuses_an_exact_match_without_posting(self):
        self._set_response([
            {"id": 4, "initiators": [IQN_A]},
        ])

        group_id = self.client.get_or_create_initiator_group([IQN_A])

        self.assertEqual(group_id, 4)
        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/initiator", timeout=DEFAULT_TIMEOUT,
        )

    def test_match_ignores_ordering(self):
        self._set_response([
            {"id": 5, "initiators": [IQN_B, IQN_A]},
        ])

        self.assertEqual(
            self.client.get_or_create_initiator_group([IQN_A, IQN_B]), 5
        )

    def test_creates_when_nothing_matches(self):
        self._set_responses([{"id": 4, "initiators": [IQN_B]}], {"id": 9})

        group_id = self.client.get_or_create_initiator_group([IQN_A])

        self.assertEqual(group_id, 9)
        self.assertEqual(self.session.request.call_count, 2)
        self.assertEqual(self._payload(1), {"initiators": [IQN_A]})

    def test_subset_is_not_a_match(self):
        # A group permitting two hosts must not be reused for one host --
        # that would silently widen access.
        self._set_responses([{"id": 4, "initiators": [IQN_A, IQN_B]}],
                            {"id": 10})

        self.assertEqual(
            self.client.get_or_create_initiator_group([IQN_A]), 10
        )

    def test_superset_is_not_a_match(self):
        self._set_responses([{"id": 4, "initiators": [IQN_A]}], {"id": 11})

        self.assertEqual(
            self.client.get_or_create_initiator_group([IQN_A, IQN_B]), 11
        )

    def test_group_with_no_initiators_is_never_reused(self):
        # An empty `initiators` list means "allow every initiator" on
        # TrueNAS. Reusing it would export the volume to the whole network.
        self._set_responses([{"id": 1, "initiators": []}], {"id": 12})

        self.assertEqual(
            self.client.get_or_create_initiator_group([IQN_A]), 12
        )

    def test_empty_request_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.client.get_or_create_initiator_group([])

        self.assertIn("allow all initiators", str(caught.exception))
        self.session.request.assert_not_called()

    def test_missing_initiators_key_does_not_crash(self):
        self._set_responses([{"id": 1}], {"id": 13})

        self.assertEqual(
            self.client.get_or_create_initiator_group([IQN_A]), 13
        )

    def test_does_not_send_auth_network(self):
        # The design spec's payload included `auth_network`. That field does
        # not exist on /iscsi/initiator -- auth_networks lives on the target.
        self._set_responses([], {"id": 14})

        self.client.get_or_create_initiator_group([IQN_A])

        self.assertNotIn("auth_network", self._payload(1))
        self.assertNotIn("auth_networks", self._payload(1))


class TestExtents(IscsiTestCase):
    """Extent creation -- the payload the shipped client got wrong twice."""

    def test_create_extent_sends_name_and_disk_only(self):
        self._set_response({"id": 2, "name": VOLUME})

        extent_id = self.client.create_extent(
            f"zvol/Dev-Pool/{VOLUME}", VOLUME
        )

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/extent",
            json={"name": VOLUME, "disk": f"zvol/Dev-Pool/{VOLUME}"},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(extent_id, 2)

    def test_create_extent_never_sends_title_case_type(self):
        # The appliance rejects type "Disk": the enum is ['DISK', 'FILE'].
        # Omitting the field entirely gets the right default.
        self._set_response({"id": 2})

        self.client.create_extent(f"zvol/Dev-Pool/{VOLUME}", VOLUME)

        self.assertNotEqual(self._payload().get("type"), "Disk")

    def test_create_extent_never_sends_the_zvol_as_path(self):
        # `path` is the FILE-extent field. Sending the zvol there fails
        # with "iscsi_extent_create.disk: This field is required".
        self._set_response({"id": 2})

        self.client.create_extent(f"zvol/Dev-Pool/{VOLUME}", VOLUME)

        self.assertNotIn("path", self._payload())

    def test_create_extent_returns_id_not_dict(self):
        self._set_response({"id": 42, "name": VOLUME, "naa": "0x6589cfc0"})

        self.assertEqual(
            self.client.create_extent("zvol/Dev-Pool/x", "x"), 42
        )

    def test_create_extent_rejects_a_response_without_an_id(self):
        self._set_response({"name": VOLUME})

        with self.assertRaises(TrueNASAPIError) as caught:
            self.client.create_extent("zvol/Dev-Pool/x", "x")

        self.assertIn("no usable id", str(caught.exception))

    def test_delete_extent_sends_no_destructive_options(self):
        # `remove` deletes the backing file and `force` deletes an extent
        # in use. Both default to false; sending nothing keeps them there.
        self._set_response({})

        self.client.delete_extent(7)

        self.session.request.assert_called_once_with(
            "DELETE", f"{BASE_URL}/iscsi/extent/id/7",
            timeout=DEFAULT_TIMEOUT,
        )

    def test_get_extents_lists(self):
        self._set_response([{"id": 1, "name": VOLUME}])

        self.assertEqual(len(self.client.get_extents()), 1)
        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/extent", timeout=DEFAULT_TIMEOUT,
        )


class TestTargets(IscsiTestCase):
    """Target creation, validation and deletion."""

    def test_create_target_binds_portal_and_initiator(self):
        self._set_response({"id": 3, "name": VOLUME})

        target_id = self.client.create_target(VOLUME, 4, 1)

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/target",
            json={
                "name": VOLUME,
                "groups": [{
                    "portal": 1,
                    "initiator": 4,
                    "authmethod": "NONE",
                }],
            },
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(target_id, 3)

    def test_create_target_uses_no_chap(self):
        # CHAP is deferred past v1 (#27) and the driver must never invent a
        # secret (#15).
        self._set_response({"id": 3})

        self.client.create_target(VOLUME, 4, 1)

        group = self._payload()["groups"][0]
        self.assertEqual(group["authmethod"], "NONE")
        self.assertNotIn("auth", group)

    def test_create_target_accepts_several_portals(self):
        # Multipath: one target, one LUN, several routes to it. Verified
        # against the appliance in #45.
        self._set_response({"id": 3})

        self.client.create_target(VOLUME, 4, [1, 2])

        self.assertEqual(
            self._payload()["groups"],
            [
                {"portal": 1, "initiator": 4, "authmethod": "NONE"},
                {"portal": 2, "initiator": 4, "authmethod": "NONE"},
            ],
        )

    def test_portal_order_is_sent_as_given(self):
        # The order we send determines which portal becomes the singular
        # target_portal in provider_location, so it must be ours, not
        # rearranged on the way out.
        self._set_response({"id": 3})

        self.client.create_target(VOLUME, 4, [2, 1])

        self.assertEqual(
            [g["portal"] for g in self._payload()["groups"]], [2, 1]
        )

    def test_every_portal_shares_one_initiator_group(self):
        # Which IQNs may connect is a property of the volume, not of the
        # path they arrive by.
        self._set_response({"id": 3})

        self.client.create_target(VOLUME, 4, [1, 2, 3])

        for group in self._payload()["groups"]:
            self.assertEqual(group["initiator"], 4)
            self.assertEqual(group["authmethod"], "NONE")

    def test_single_portal_still_accepted_as_a_bare_int(self):
        self._set_response({"id": 3})

        self.client.create_target(VOLUME, 4, 1)

        self.assertEqual(
            self._payload()["groups"],
            [{"portal": 1, "initiator": 4, "authmethod": "NONE"}],
        )

    def test_single_portal_in_a_list_is_equivalent(self):
        self._set_response({"id": 3})
        self.client.create_target(VOLUME, 4, [1])
        as_list = self._payload()["groups"]

        self.session.reset_mock()
        self._set_response({"id": 3})
        self.client.create_target(VOLUME, 4, 1)

        self.assertEqual(as_list, self._payload()["groups"])

    def test_no_portal_is_refused(self):
        # A target with no portal group is unreachable by any initiator.
        with self.assertRaises(ValueError) as caught:
            self.client.create_target(VOLUME, 4, [])

        self.assertIn("at least one portal", str(caught.exception))
        self.session.request.assert_not_called()

    def test_duplicate_portal_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.client.create_target(VOLUME, 4, [1, 2, 1])

        self.assertIn("repeated portal", str(caught.exception))
        self.session.request.assert_not_called()

    def test_bool_portal_id_is_refused(self):
        # bool is an int subclass, so True would otherwise be wrapped into
        # [True] and serialise as JSON `true` for the portal id (#51).
        for value in (True, False):
            self.session.reset_mock()
            with self.assertRaises(ValueError) as caught:
                self.client.create_target(VOLUME, 4, value)

            self.assertIn("portal id", str(caught.exception))
            self.session.request.assert_not_called()

    def test_bool_inside_a_portal_list_is_refused(self):
        with self.assertRaises(ValueError):
            self.client.create_target(VOLUME, 4, [1, True])

        self.session.request.assert_not_called()

    def test_non_integer_portal_id_is_refused(self):
        for value in ("1", None, 1.0):
            self.session.reset_mock()
            with self.assertRaises(ValueError):
                self.client.create_target(VOLUME, 4, [value])

            self.session.request.assert_not_called()

    def test_bare_non_iterable_portal_id_is_refused(self):
        # Not wrapped in a list: None has no __iter__, so it must normalise
        # to [None] and be refused, not blow up in list(None).
        for value in (None, 1.5):
            self.session.reset_mock()
            with self.assertRaises(ValueError):
                self.client.create_target(VOLUME, 4, value)

            self.session.request.assert_not_called()

    def test_returns_only_the_id_never_the_reordered_groups(self):
        # The appliance returns groups in a different order than they were
        # sent (verified in #45). Returning the dict would hand a caller a
        # reordered list that looks authoritative; returning the id alone
        # means there is nothing to misuse.
        self._set_response({
            "id": 3,
            "name": VOLUME,
            "groups": [
                {"portal": 2, "initiator": 4, "authmethod": "NONE"},
                {"portal": 1, "initiator": 4, "authmethod": "NONE"},
            ],
        })

        result = self.client.create_target(VOLUME, 4, [1, 2])

        self.assertEqual(result, 3)
        self.assertNotIsInstance(result, dict)

    def test_delete_target_never_sends_delete_extents(self):
        # `delete_extents: true` would widen an export teardown into
        # destroying the extent behind a volume that still exists.
        self._set_response({})

        self.client.delete_target(3)

        self.session.request.assert_called_once_with(
            "DELETE", f"{BASE_URL}/iscsi/target/id/3",
            timeout=DEFAULT_TIMEOUT,
        )

    def test_validate_target_name_returns_none_when_acceptable(self):
        self._set_response(None)

        self.assertIsNone(self.client.validate_target_name(VOLUME))
        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/target/validate_name",
            json={"name": VOLUME},
            timeout=DEFAULT_TIMEOUT,
        )

    def test_get_targets_lists(self):
        self._set_response([{"id": 3, "name": VOLUME, "groups": []}])

        self.assertEqual(len(self.client.get_targets()), 1)
        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/target", timeout=DEFAULT_TIMEOUT,
        )

    def test_validate_target_name_returns_the_reason_when_not(self):
        reason = ("Only lowercase alphanumeric characters plus dot (.), "
                  "dash (-), and colon (:) are allowed.")
        self._set_response(reason)

        self.assertEqual(
            self.client.validate_target_name("Volume_1"), reason
        )


class TestTargetExtents(IscsiTestCase):
    """Target-to-extent association."""

    def test_create_target_extent_posts_ids(self):
        self._set_response({"id": 7, "target": 1, "extent": 2, "lunid": 3})

        link_id = self.client.create_target_extent(
            target_id=1, extent_id=2, lun_id=3
        )

        self.session.request.assert_called_once_with(
            "POST",
            f"{BASE_URL}/iscsi/targetextent",
            json={"target": 1, "extent": 2, "lunid": 3},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(link_id, 7)

    def test_lun_defaults_to_zero(self):
        self._set_response({"id": 7})

        self.client.create_target_extent(target_id=1, extent_id=2)

        self.assertEqual(self._payload()["lunid"], 0)

    def test_get_target_extents_lists(self):
        self._set_response([
            {"id": 3, "target": 3, "extent": 3, "lunid": 0},
        ])

        self.assertEqual(len(self.client.get_target_extents()), 1)
        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/targetextent", timeout=DEFAULT_TIMEOUT,
        )

    def test_delete_target_extent_uses_the_id_path(self):
        self._set_response({})

        self.client.delete_target_extent(7)

        self.session.request.assert_called_once_with(
            "DELETE", f"{BASE_URL}/iscsi/targetextent/id/7",
            timeout=DEFAULT_TIMEOUT,
        )

    def test_already_cascaded_link_surfaces_as_not_found(self):
        # Deleting either end cascades the link, so an explicit delete
        # afterwards hits 422/errno 2. That must map to NotFound, which is
        # what makes best_effort_delete swallow it.
        self._set_error(422, {
            "null": [
                {"message": "iSCSITargetToExtent 7 does not exist",
                 "errno": 2}
            ]
        })

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.delete_target_extent(7)


class TestIscsiService(IscsiTestCase):
    """Service state -- the difference between reload and start."""

    def test_get_iscsi_service_filters_and_unwraps(self):
        self._set_response([
            {"id": 7, "service": "iscsitarget", "enable": False,
             "state": "STOPPED"},
        ])

        service = self.client.get_iscsi_service()

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/service",
            params={"service": "iscsitarget"},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(service["state"], "STOPPED")

    def test_get_iscsi_service_raises_when_absent(self):
        self._set_response([])

        with self.assertRaises(TrueNASAPIError) as caught:
            self.client.get_iscsi_service()

        self.assertIn("iscsitarget", str(caught.exception))

    def test_start_posts_the_service_name(self):
        self._set_response(True)

        self.assertTrue(self.client.start_iscsi_service())
        self.session.request.assert_called_once_with(
            "POST", f"{BASE_URL}/service/start",
            json={"service": "iscsitarget"},
            timeout=DEFAULT_TIMEOUT,
        )

    def test_reload_posts_the_service_name(self):
        self._set_response(True)

        self.assertTrue(self.client.reload_iscsi_service())
        self.session.request.assert_called_once_with(
            "POST", f"{BASE_URL}/service/reload",
            json={"service": "iscsitarget"},
            timeout=DEFAULT_TIMEOUT,
        )

    def test_reload_reports_false_against_a_stopped_service(self):
        # The appliance answers `false` and leaves the service stopped.
        # Returning that verbatim is what lets a caller notice the config
        # it just wrote is inert.
        self._set_response(False)

        self.assertFalse(self.client.reload_iscsi_service())


class TestNameLookup(IscsiTestCase):
    """Name-based lookup -- the authoritative teardown path (#16).

    The appliance rejects an invalid filter *operator* with a 422 but
    accepts an unrecognised filter *field*, answering 200 with an empty
    list. So "it returned rows" is not by itself evidence the filter was
    applied, and these tests pin the filter being sent and a multi-row
    answer being refused rather than silently indexed.
    """

    def test_get_target_by_name_filters_server_side(self):
        self._set_response([{"id": 6, "name": VOLUME}])

        found = self.client.get_target_by_name(VOLUME)

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/target",
            params={"name": VOLUME},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(found["id"], 6)

    def test_get_extent_by_name_filters_server_side(self):
        self._set_response([{"id": 6, "name": VOLUME}])

        found = self.client.get_extent_by_name(VOLUME)

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/extent",
            params={"name": VOLUME},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(found["id"], 6)

    def test_missing_target_returns_none(self):
        self._set_response([])

        self.assertIsNone(self.client.get_target_by_name(VOLUME))

    def test_missing_extent_returns_none(self):
        self._set_response([])

        self.assertIsNone(self.client.get_extent_by_name(VOLUME))

    def test_multiple_matches_raise_rather_than_guess(self):
        # Two rows means the filter was ignored and we are looking at the
        # whole collection. Taking [0] would return another volume's
        # export, and the caller deletes what it is handed.
        self._set_response([
            {"id": 6, "name": VOLUME},
            {"id": 7, "name": "volume-other"},
        ])

        with self.assertRaises(TrueNASAPIError) as caught:
            self.client.get_target_by_name(VOLUME)

        self.assertIn("ignored", str(caught.exception))

    def test_multiple_extent_matches_raise(self):
        self._set_response([
            {"id": 6, "name": VOLUME},
            {"id": 7, "name": "volume-other"},
        ])

        with self.assertRaises(TrueNASAPIError):
            self.client.get_extent_by_name(VOLUME)

    def test_multi_match_error_carries_request_context(self):
        self._set_response([{"id": 6}, {"id": 7}])

        with self.assertRaises(TrueNASAPIError) as caught:
            self.client.get_extent_by_name(VOLUME)

        self.assertEqual(caught.exception.endpoint, "/iscsi/extent")
        self.assertEqual(caught.exception.method, "GET")

    def test_lookup_never_fetches_the_unfiltered_collection(self):
        # A lookup that fell back to listing everything and filtering in
        # Python would still pass the happy-path tests above.
        for lookup in (self.client.get_target_by_name,
                       self.client.get_extent_by_name):
            self.session.reset_mock()
            self._set_response([{"id": 6, "name": VOLUME}])

            lookup(VOLUME)

            self.assertEqual(self.session.request.call_count, 1)
            kwargs = self.session.request.call_args.kwargs
            self.assertEqual(kwargs.get("params"), {"name": VOLUME})

    def test_lookup_is_a_read(self):
        for lookup in (self.client.get_target_by_name,
                       self.client.get_extent_by_name):
            self.session.reset_mock()
            self._set_response([])

            lookup(VOLUME)

            self.assertEqual(self.session.request.call_args.args[0], "GET")


class TestTargetExtentLookup(IscsiTestCase):
    """Finding the association between a specific target and extent."""

    def test_filters_on_both_ids(self):
        self._set_response([{"id": 25, "target": 9, "extent": 8}])

        found = self.client.get_target_extent(9, 8)

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/iscsi/targetextent",
            params={"target": 9, "extent": 8},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(found["id"], 25)

    def test_returns_none_when_unlinked(self):
        self._set_response([])

        self.assertIsNone(self.client.get_target_extent(9, 8))

    def test_refuses_multiple_matches(self):
        # More than one means the filter was ignored and the whole
        # collection came back -- same reasoning as _get_one_by_name.
        self._set_response([{"id": 25}, {"id": 26}])

        with self.assertRaises(TrueNASAPIError) as caught:
            self.client.get_target_extent(9, 8)

        self.assertIn("ignored", str(caught.exception))

    def test_is_a_read(self):
        self._set_response([])

        self.client.get_target_extent(9, 8)

        self.assertEqual(self.session.request.call_args.args[0], "GET")


class TestTargetGroups(IscsiTestCase):
    """The groups payload, shared by create and update."""

    def test_single_portal_becomes_one_group(self):
        self.assertEqual(
            self.client.target_groups(4, 1),
            [{"portal": 1, "initiator": 4, "authmethod": "NONE"}],
        )

    def test_several_portals_keep_their_order(self):
        self.assertEqual(
            [g["portal"] for g in self.client.target_groups(4, [2, 1])],
            [2, 1],
        )

    def test_every_group_shares_the_initiator_and_uses_no_chap(self):
        for group in self.client.target_groups(4, [1, 2, 3]):
            self.assertEqual(group["initiator"], 4)
            self.assertEqual(group["authmethod"], "NONE")

    def test_create_target_uses_the_same_builder(self):
        # If these drift, a target updated later gets a different shape
        # than the one it was created with.
        self._set_response({"id": 3})

        self.client.create_target(VOLUME, 4, [1, 2])

        self.assertEqual(self._payload()["groups"],
                         self.client.target_groups(4, [1, 2]))


class TestUpdateTargetGroups(IscsiTestCase):
    """Repointing an adopted target at the attaching host (#62)."""

    def test_puts_only_the_groups(self):
        self._set_response({"id": 9})

        self.client.update_target_groups(9, 4, 1)

        self.session.request.assert_called_once_with(
            "PUT", f"{BASE_URL}/iscsi/target/id/9",
            json={"groups": [{"portal": 1, "initiator": 4,
                              "authmethod": "NONE"}]},
            timeout=DEFAULT_TIMEOUT,
        )

    def test_accepts_several_portals(self):
        self._set_response({"id": 9})

        self.client.update_target_groups(9, 4, [1, 2])

        self.assertEqual(
            [g["portal"] for g in self._payload()["groups"]], [1, 2])

    def test_does_not_send_the_name(self):
        # A PUT carrying `name` would rename the target.
        self._set_response({"id": 9})

        self.client.update_target_groups(9, 4, 1)

        self.assertNotIn("name", self._payload())

    def test_missing_target_surfaces_as_not_found(self):
        self._set_error(422, {
            "null": [{"message": "iSCSITarget 9 does not exist",
                      "errno": 2}]
        })

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.update_target_groups(9, 4, 1)


class TestPackageVersion(unittest.TestCase):
    """The version must not drift between pyproject.toml and the package."""

    def test_version_is_a_release_string(self):
        self.assertRegex(truenas_cinder_driver.__version__,
                         r"^\d+\.\d+\.\d+$")

    def test_installed_metadata_matches_the_package(self):
        # pyproject.toml reads __version__ dynamically, so these can only
        # disagree if that wiring breaks. Skipped when running from a
        # checkout without an install -- the nix dev shell does exactly
        # that, and CI installs with `pip install -e .` so it runs there.
        try:
            declared = importlib.metadata.version("truenas-cinder-driver")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("package not installed; nothing to compare")

        self.assertEqual(declared, truenas_cinder_driver.__version__)


SNAP = "snapshot-7f2c1b9e-3a44-4d61-8e05-9b7c2f0a1d38"
DATASET = f"Dev-Pool/{VOLUME}"
SNAPSHOT_ID = f"{DATASET}@{SNAP}"
# Transcribed from the appliance: deleting a snapshot that is not there.
SNAPSHOT_ENOENT_BODY = {
    "null": [
        {"message": f"Snapshot {SNAPSHOT_ID} not found", "errno": 2}
    ]
}


class TestSnapshotOperations(IscsiTestCase):
    """Snapshots live under /pool/snapshot, with percent-encoded ids (#42)."""

    def test_snapshot_id_joins_with_an_at_sign(self):
        self.assertEqual(
            self.client.snapshot_id("Dev-Pool", VOLUME, SNAP), SNAPSHOT_ID
        )

    def test_get_snapshot_list_unfiltered(self):
        self._set_response([])

        self.client.get_snapshot_list()

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/pool/snapshot", timeout=DEFAULT_TIMEOUT,
        )

    def test_get_snapshot_list_filters_by_dataset(self):
        # Unfiltered, this returns the appliance's boot-pool snapshots too.
        self._set_response([])

        self.client.get_snapshot_list(dataset=DATASET)

        self.session.request.assert_called_once_with(
            "GET", f"{BASE_URL}/pool/snapshot",
            params={"dataset": DATASET},
            timeout=DEFAULT_TIMEOUT,
        )

    def test_create_snapshot_posts_to_pool_snapshot(self):
        self._set_response({"id": SNAPSHOT_ID})

        result = self.client.create_snapshot(DATASET, SNAP)

        self.session.request.assert_called_once_with(
            "POST", f"{BASE_URL}/pool/snapshot",
            json={"dataset": DATASET, "name": SNAP},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(result["id"], SNAPSHOT_ID)

    def test_create_snapshot_merges_extra_options(self):
        self._set_response({"id": SNAPSHOT_ID})

        self.client.create_snapshot(DATASET, SNAP, recursive=True)

        self.assertTrue(self._payload()["recursive"])

    def test_delete_snapshot_percent_encodes_the_id(self):
        # A raw id turns '/' into extra path segments and 404s -- which the
        # client maps to NotFound, which an idempotent caller swallows. That
        # is how the pre-#42 method deleted nothing and reported success.
        self._set_response({})

        self.client.delete_snapshot(SNAPSHOT_ID)

        url = self.session.request.call_args.args[1]
        self.assertIn("%2F", url)
        self.assertIn("%40", url)
        self.assertNotIn(f"/{VOLUME}@", url)

    def test_delete_snapshot_uses_the_expected_url(self):
        self._set_response({})

        self.client.delete_snapshot(SNAPSHOT_ID)

        encoded = SNAPSHOT_ID.replace("/", "%2F").replace("@", "%40")
        self.session.request.assert_called_once_with(
            "DELETE", f"{BASE_URL}/pool/snapshot/id/{encoded}",
            json={"defer": False},
            timeout=DEFAULT_TIMEOUT,
        )

    def test_delete_snapshot_defers_when_asked(self):
        self._set_response({})

        self.client.delete_snapshot(SNAPSHOT_ID, defer=True)

        self.assertTrue(self._payload()["defer"])

    def test_missing_snapshot_delete_maps_to_not_found(self):
        # 422/errno 2, same shape as a missing dataset -- this is what makes
        # an idempotent delete_snapshot possible.
        self._set_error(422, SNAPSHOT_ENOENT_BODY)

        with self.assertRaises(TrueNASAPINotFoundError):
            self.client.delete_snapshot(SNAPSHOT_ID)

    def test_no_snapshot_method_touches_the_legacy_zfs_path(self):
        # /zfs/snapshot is a 404 on 25.10.5. It answers in *plain text*, so
        # a caller swallowing NotFound for idempotency reads a dead path as
        # a successful delete -- this guards the whole family at once.
        for call in (
            lambda: self.client.get_snapshot_list(),
            lambda: self.client.get_snapshot_list(dataset=DATASET),
            lambda: self.client.create_snapshot(DATASET, SNAP),
            lambda: self.client.delete_snapshot(SNAPSHOT_ID),
        ):
            self.session.reset_mock()
            self._set_response({"id": SNAPSHOT_ID})
            call()
            url = self.session.request.call_args.args[1]
            self.assertNotIn("/zfs/", url)
            self.assertIn("/pool/snapshot", url)


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

    def test_timeout_message_names_both_halves_of_the_budget(self):
        # "(10.0, 60.0)s" hides which half was exceeded.
        self.session.request.side_effect = requests.ReadTimeout("slow")

        with self.assertRaises(TrueNASAPITimeoutError) as ctx:
            self.client.get_pool_list()

        self.assertIn("connect 10.0s, read 60.0s", str(ctx.exception))

    def test_scalar_timeout_rendered_plainly(self):
        client = TrueNASAPIClient(
            base_url="https://nas.internal", api_key=API_KEY, timeout=30
        )
        client.session = mock.MagicMock()
        client.session.request.side_effect = requests.ReadTimeout("slow")

        with self.assertRaises(TrueNASAPITimeoutError) as ctx:
            client.get_pool_list()

        self.assertIn("30s", str(ctx.exception))

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
