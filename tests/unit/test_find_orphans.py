"""The reconciliation CLI (#48).

`tools/find_orphans.py` is not a package, so it is loaded by path. No
network: everything is mocked at the `requests` boundary, the same
place `test_api_client.py` mocks.

The host match and the exit code get the most attention. The first
decides what counts as a leak and therefore what `--delete-exports`
offers to remove; the second is what an automated caller acts on.
"""

import importlib.util
import os
import pathlib
import unittest
from unittest import mock

from truenas_cinder_driver.api_client import TrueNASAPIError


def _load():
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "tools" / "find_orphans.py")
    spec = importlib.util.spec_from_file_location("find_orphans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


orphans = _load()

HOST = "controller@truenas-iscsi"
UUID = "4d9e1a5c-8f3b-4a21-9c77-2e6b0f1d3a84"


def _response(status, body=None, headers=None):
    response = mock.MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = body
    response.text = ""
    return response


class TestLoadEnv(unittest.TestCase):
    """The file wins over the shell, deliberately."""

    def test_the_file_overrides_an_exported_value(self):
        # With `setdefault`, a stale export would silently outrank the
        # file the operator just edited -- and with --delete-exports that
        # is the difference between a report and an outage.
        with mock.patch.dict(os.environ, {"TRUENAS_API_URL": "http://stale"}):
            with mock.patch.object(orphans.pathlib, "Path") as fake_path:
                fake_path.return_value.exists.return_value = True
                fake_path.return_value.read_text.return_value = (
                    "TRUENAS_API_URL=http://fresh\n")
                orphans.load_env("whatever")

                self.assertEqual(os.environ["TRUENAS_API_URL"],
                                 "http://fresh")

    def test_a_missing_file_is_not_an_error(self):
        with mock.patch.object(orphans.pathlib, "Path") as fake_path:
            fake_path.return_value.exists.return_value = False

            orphans.load_env("absent")          # must not raise

    def test_comments_and_blank_lines_are_skipped(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            with mock.patch.object(orphans.pathlib, "Path") as fake_path:
                fake_path.return_value.exists.return_value = True
                fake_path.return_value.read_text.return_value = (
                    "# a comment=not a var\n\nKEY_UNDER_TEST=value\n")
                orphans.load_env("whatever")

                self.assertEqual(os.environ["KEY_UNDER_TEST"], "value")
                self.assertNotIn("# a comment", os.environ)


class TestKeystoneToken(unittest.TestCase):
    """Both auth shapes, because they are not interchangeable."""

    ENV_APP = {
        "OS_AUTH_URL": "http://keystone:5000/v3/",
        "OS_APPLICATION_CREDENTIAL_ID": "abc",
        "OS_APPLICATION_CREDENTIAL_SECRET": "shh",
    }
    ENV_PASSWORD = {
        "OS_AUTH_URL": "http://keystone:5000/v3",
        "OS_USERNAME": "admin",
        "OS_PASSWORD": "secret",
        "OS_PROJECT_NAME": "admin",
    }

    def _post(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(orphans.requests, "post") as post:
                post.return_value = _response(
                    201, {"token": {"catalog": []}},
                    headers={"X-Subject-Token": "tok"})
                orphans.keystone_token()
                return post.call_args

    def test_an_application_credential_body(self):
        args = self._post(self.ENV_APP)

        identity = args.kwargs["json"]["auth"]["identity"]
        self.assertEqual(identity["methods"], ["application_credential"])
        self.assertEqual(identity["application_credential"]["id"], "abc")
        # Application credentials are already scoped and reject a scope.
        self.assertNotIn("scope", args.kwargs["json"]["auth"])

    def test_a_password_body_carries_a_scope(self):
        args = self._post(self.ENV_PASSWORD)

        body = args.kwargs["json"]["auth"]
        self.assertEqual(body["identity"]["methods"], ["password"])
        self.assertEqual(body["scope"]["project"]["name"], "admin")

    def test_a_trailing_slash_on_the_auth_url_is_not_doubled(self):
        args = self._post(self.ENV_APP)

        self.assertEqual(args.args[0], "http://keystone:5000/v3/auth/tokens")

    def test_rejected_credentials_exit_rather_than_continue(self):
        with mock.patch.dict(os.environ, self.ENV_APP, clear=True):
            with mock.patch.object(orphans.requests, "post") as post:
                post.return_value = _response(401)

                with self.assertRaises(SystemExit):
                    orphans.keystone_token()


class TestCinderVolumeNames(unittest.TestCase):
    """The host match decides what counts as a leak."""

    def _names(self, volumes, backend=HOST):
        with mock.patch.object(orphans, "keystone_token") as token:
            token.return_value = ("tok", {"catalog": [{
                "type": "volumev3",
                "endpoints": [{"interface": "public",
                               "url": "http://cinder/v3/p"}]}]})
            with mock.patch.object(orphans.requests, "get") as get:
                get.return_value = _response(200, {"volumes": volumes})
                result = orphans.cinder_volume_names(backend)
                return result, get.call_args

    def test_a_volume_on_this_backend_is_known(self):
        (names, _), _args = self._names(
            [{"id": UUID, "os-vol-host-attr:host": HOST}])

        self.assertEqual(names, {"volume-%s" % UUID})

    def test_the_pool_suffix_is_ignored_on_both_sides(self):
        # Cinder reports host@backend#pool; an operator may pass either.
        (names, _), _args = self._names(
            [{"id": UUID, "os-vol-host-attr:host": f"{HOST}#truenas-iscsi"}],
            backend=f"{HOST}#truenas-iscsi")

        self.assertEqual(names, {"volume-%s" % UUID})

    def test_a_volume_on_another_backend_is_not_known(self):
        # The consequence of getting this wrong: that volume's export
        # looks like a leak, and --delete-exports offers to remove it.
        (names, _), _args = self._names(
            [{"id": UUID, "os-vol-host-attr:host": "other@lvm"}])

        self.assertEqual(names, set())

    def test_a_volume_with_no_host_attribute_is_not_claimed(self):
        (names, _), _args = self._names([{"id": UUID}])

        self.assertEqual(names, set())

    def test_every_project_is_asked_for(self):
        # Without all_tenants another project's volumes are invisible to
        # this query and therefore look like leaks.
        _result, args = self._names([])

        self.assertEqual(args.kwargs["params"], {"all_tenants": 1})

    def test_the_total_counts_every_volume_not_just_ours(self):
        (_names, total), _args = self._names([
            {"id": UUID, "os-vol-host-attr:host": HOST},
            {"id": "other", "os-vol-host-attr:host": "other@lvm"},
        ])

        self.assertEqual(total, 2)

    def test_a_refused_volume_list_exits_rather_than_reporting_leaks(self):
        # Continuing with an empty set would report every object on the
        # appliance as unaccounted for.
        with mock.patch.object(orphans, "keystone_token") as token:
            token.return_value = ("tok", {"catalog": [{
                "type": "volumev3",
                "endpoints": [{"interface": "public", "url": "http://c"}]}]})
            with mock.patch.object(orphans.requests, "get") as get:
                get.return_value = _response(403)

                with self.assertRaises(SystemExit):
                    orphans.cinder_volume_names(HOST)

    def test_a_catalog_with_no_volume_endpoint_exits(self):
        with mock.patch.object(orphans, "keystone_token") as token:
            token.return_value = ("tok", {"catalog": []})

            with self.assertRaises(SystemExit):
                orphans.cinder_volume_names(HOST)


class TestDeleteExports(unittest.TestCase):
    """What it removes, in what order, and what it will not touch."""

    REPORT = {
        "dangling_links": [{"id": 3, "target": 1, "extent": 2}],
        "leaked_targets": [{"id": 1, "name": "volume-a"}],
        "leaked_extents": [{"id": 2, "name": "volume-a"}],
        "unlinked_extents": [{"id": 4, "name": "volume-b"}],
        "leaked_zvols": [{"name": "Dev-Pool/volume-c"}],
        "duplicate_initiator_groups": [],
        "unused_initiator_groups": [],
        "adoptable_zvols": [],
    }

    def test_links_go_before_targets_before_extents(self):
        client = mock.MagicMock()
        order = []
        client.delete_target_extent.side_effect = (
            lambda i: order.append("link"))
        client.delete_target.side_effect = lambda i: order.append("target")
        client.delete_extent.side_effect = lambda i: order.append("extent")

        orphans.delete_exports(client, self.REPORT)

        self.assertEqual(order, ["link", "target", "extent", "extent"])

    def test_no_zvol_is_ever_removed(self):
        # The rule the whole tool is built around. A zvol is the disk.
        client = mock.MagicMock()

        orphans.delete_exports(client, self.REPORT)

        client.delete_zvol.assert_not_called()
        client.delete_snapshot.assert_not_called()

    def test_the_counts_separate_success_from_failure(self):
        client = mock.MagicMock()
        client.delete_target.side_effect = TrueNASAPIError("busy")

        removed, failed = orphans.delete_exports(client, self.REPORT)

        self.assertEqual(failed, 1)
        self.assertEqual(removed, 3)

    def test_one_failure_does_not_abandon_the_rest(self):
        client = mock.MagicMock()
        client.delete_target_extent.side_effect = TrueNASAPIError("nope")

        orphans.delete_exports(client, self.REPORT)

        # The extents are still attempted after the link failed.
        self.assertEqual(client.delete_extent.call_count, 2)
