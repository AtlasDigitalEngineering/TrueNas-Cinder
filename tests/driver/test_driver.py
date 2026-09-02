"""
Unit tests for the TrueNAS Cinder driver skeleton.

These require Cinder to be installed -- see ``tests/driver/__init__.py``
for why they live apart from ``tests/unit``.

The API client is mocked at the client boundary rather than at
``requests``: the client's own behaviour is covered exhaustively in
``tests/unit/test_api_client.py`` and verified against real hardware, so
what matters here is that the driver asks the right questions and reacts
correctly to the answers.

Several tests assert on message *content*. That is deliberate rather than
brittle: the point of the setup checks is that a deploying engineer can fix
the problem from the message alone, so a message that stops naming the
config option has regressed even if it still raises.
"""

import pathlib
import unittest
from unittest import mock

from cinder import coordination
from cinder import exception

import truenas_cinder_driver
from truenas_cinder_driver import api_client
from truenas_cinder_driver import driver as tnd


POOL = 'Dev-Pool'
API_URL = 'https://truenas.example.com'
API_KEY = '1-notarealkey'


class FakeConfiguration(object):
    """Stands in for Cinder's per-backend Configuration object."""

    def __init__(self, **values):
        self._values = dict(values)

    def append_config_values(self, opts):
        for opt in opts:
            self._values.setdefault(opt.name, opt.default)

    def safe_get(self, name):
        return self._values.get(name)

    def __getattr__(self, name):
        try:
            return self.__dict__['_values'][name]
        except KeyError:
            raise AttributeError(name)


class DriverTestCase(unittest.TestCase):
    """Base fixture: a driver with a mocked API client."""

    def setUp(self):
        """Stand in for the coordinator Cinder starts in the service.

        `coordination.COORDINATOR.get_lock` returns None when the
        coordinator has not been started, and the decorator then fails
        inside a `with`. Tests are not the place to discover that.

        The stub is deliberately *observable* -- `self.locks` records the
        names requested -- so a test can assert a lock was actually taken.
        A stub that silently swallowed every acquisition would make the
        decorators invisible, and removing one would break nothing.
        """
        patcher = mock.patch.object(coordination.COORDINATOR, 'get_lock')
        self.locks = patcher.start()
        self.locks.return_value = mock.MagicMock()
        self.addCleanup(patcher.stop)

    def lock_names(self):
        """Return the lock names acquired so far, as strings."""
        return [call.args[0] for call in self.locks.call_args_list]

    def _configuration(self, **over):
        values = dict(
            truenas_api_url=API_URL,
            truenas_api_key=API_KEY,
            truenas_pool=POOL,
            truenas_verify_ssl=True,
            volume_backend_name='truenas-iscsi',
            # SanDriver reads this during __init__.
            san_is_local=False,
        )
        values.update(over)
        return FakeConfiguration(**values)

    def _driver(self, **over):
        driver = tnd.TrueNASISCSIDriver(
            configuration=self._configuration(**over))
        driver.client = mock.MagicMock()
        driver.client.get_pool_list.return_value = [
            {'name': POOL, 'size': 105763569664, 'free': 103634919424},
        ]
        driver.client.get_iscsi_service.return_value = {
            'service': 'iscsitarget', 'state': 'RUNNING', 'enable': True,
        }
        driver.client.get_portals.return_value = [
            {'id': 1, 'listen': [{'ip': '10.20.21.81', 'port': 3260}],
             'tag': 1},
        ]
        driver.client.validate_target_name.return_value = None
        return driver


class TestConfigOptions(DriverTestCase):
    """Option registration."""

    def test_api_key_is_marked_secret(self):
        # Otherwise oslo_config prints it in logged config dumps.
        opt = self._opt('truenas_api_key')

        self.assertTrue(opt.secret)

    def test_other_options_are_not_secret(self):
        for name in ('truenas_api_url', 'truenas_pool',
                     'truenas_verify_ssl'):
            self.assertFalse(self._opt(name).secret, name)

    @staticmethod
    def _opt(name):
        return next(o for o in tnd.truenas_opts if o.name == name)

    def test_get_driver_options_exposes_them(self):
        names = {opt.name for opt in
                 tnd.TrueNASISCSIDriver.get_driver_options()}

        self.assertEqual(names, {opt.name for opt in tnd.truenas_opts})

    def test_verify_ssl_defaults_to_true(self):
        self.assertTrue(self._opt('truenas_verify_ssl').default)

    def test_verify_ssl_is_a_bool_option(self):
        # The abandoned draft called .lower() on this and crashed.
        self.assertIsInstance(self._opt('truenas_verify_ssl').default, bool)

    def test_portal_addresses_default_to_empty(self):
        self.assertEqual(self._opt('truenas_iscsi_portal_addresses').default,
                         [])


class TestDoSetup(DriverTestCase):
    """Client construction."""

    def test_builds_a_client_from_configuration(self):
        driver = tnd.TrueNASISCSIDriver(
            configuration=self._configuration())

        driver.do_setup(None)

        self.assertIsInstance(driver.client, api_client.TrueNASAPIClient)

    def test_missing_options_are_named(self):
        driver = tnd.TrueNASISCSIDriver(
            configuration=self._configuration(truenas_api_url=None,
                                              truenas_pool=None))

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.do_setup(None)

        message = str(caught.exception)
        self.assertIn('truenas_api_url', message)
        self.assertIn('truenas_pool', message)

    def test_inline_credentials_in_url_are_rejected(self):
        driver = tnd.TrueNASISCSIDriver(
            configuration=self._configuration(
                truenas_api_url='https://user:pass@truenas.example.com'))

        with self.assertRaises(exception.InvalidInput):
            driver.do_setup(None)

    def test_do_setup_makes_no_requests(self):
        # Validation belongs to check_for_setup_error.
        driver = tnd.TrueNASISCSIDriver(
            configuration=self._configuration())

        with mock.patch.object(api_client.TrueNASAPIClient,
                               '_make_request') as request:
            driver.do_setup(None)

        request.assert_not_called()


class TestAuthFailureMessages(DriverTestCase):
    """The line an operator reads when the service will not start.

    #59 is the case that matters: a Sharing Admin key produced "check that
    it is a valid, unrevoked key", so the obvious next step was to reissue
    the key -- which cannot help, because the key was never the problem.
    """

    def _fail_with(self, status):
        driver = self._driver()
        driver.client.get_pool_list.side_effect = (
            api_client.TrueNASAPIAuthError('HTTP %s' % status,
                                           status_code=status))
        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()
        return str(caught.exception)

    def test_403_tells_the_operator_not_to_reissue_the_key(self):
        message = self._fail_with(403)

        self.assertIn('do not reissue', message)
        self.assertIn('FULL_ADMIN', message)

    def test_401_tells_the_operator_to_issue_a_new_key(self):
        message = self._fail_with(401)

        self.assertIn('Issue a new key', message)
        self.assertIn('not a role problem', message)

    def test_the_two_messages_are_not_interchangeable(self):
        # A wrapper that mentioned both remedies would satisfy each
        # assertion above while telling the operator to try both things --
        # which is what the single previous message effectively did.
        unauthorised = self._fail_with(401)
        forbidden = self._fail_with(403)

        self.assertNotEqual(unauthorised, forbidden)
        self.assertNotIn('do not reissue', unauthorised)
        self.assertNotIn('Issue a new key', forbidden)

    def test_an_auth_error_with_no_status_is_treated_as_a_bad_key(self):
        # status_code is optional on the exception. Defaulting to the
        # role branch would tell someone their roles are wrong on the
        # strength of no evidence at all.
        message = self._fail_with(None)

        self.assertIn('Issue a new key', message)


class TestSetupValidation(DriverTestCase):
    """check_for_setup_error, one failure mode at a time."""

    def test_passes_when_everything_is_in_order(self):
        driver = self._driver()

        driver.check_for_setup_error()

        self.assertEqual(driver.portal_id, 1)
        self.assertEqual(driver.portal_addresses, ['10.20.21.81'])

    def test_does_not_require_ssh_configuration(self):
        # SanDriver.check_for_setup_error() demands san_ip plus a password
        # or private key, because it drives arrays over SSH. This driver
        # uses the REST API and must not inherit that requirement -- if
        # someone "restores" the super() call, this fails.
        driver = self._driver(san_ip='', san_password='',
                              san_private_key='')

        driver.check_for_setup_error()

    def test_bad_api_key_names_the_option(self):
        driver = self._driver()
        driver.client.get_pool_list.side_effect = (
            api_client.TrueNASAPIAuthError('HTTP 401'))

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        self.assertIn('truenas_api_key', str(caught.exception))

    def test_unreachable_appliance_is_a_backend_error(self):
        # Not InvalidInput: the configuration may be perfectly correct.
        driver = self._driver()
        driver.client.get_pool_list.side_effect = (
            api_client.TrueNASAPIConnectionError('no route'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.check_for_setup_error()

    def test_missing_pool_lists_what_is_available(self):
        driver = self._driver(truenas_pool='NoSuchPool')

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        message = str(caught.exception)
        self.assertIn('NoSuchPool', message)
        self.assertIn(POOL, message)

    def test_stopped_iscsi_service_is_refused(self):
        # The most consequential check: with the service stopped the driver
        # writes targets and extents successfully and nothing attaches.
        driver = self._driver()
        driver.client.get_iscsi_service.return_value = {
            'state': 'STOPPED', 'enable': False}

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        message = str(caught.exception)
        self.assertIn('STOPPED', message)
        self.assertIn('does not start it', message)

    def test_service_not_enabled_at_boot_only_warns(self):
        driver = self._driver()
        driver.client.get_iscsi_service.return_value = {
            'state': 'RUNNING', 'enable': False}

        with mock.patch.object(tnd, 'LOG') as log:
            driver.check_for_setup_error()

        self.assertTrue(log.warning.called)

    def test_no_portal_is_refused(self):
        driver = self._driver()
        driver.client.get_portals.return_value = []

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        self.assertIn('does not create one', str(caught.exception))

    def test_configured_portal_that_does_not_exist_is_refused(self):
        driver = self._driver(truenas_iscsi_portal_id=99)

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        self.assertIn('99', str(caught.exception))

    def test_several_portals_without_a_choice_is_refused(self):
        driver = self._driver()
        driver.client.get_portals.return_value = [
            {'id': 1, 'listen': [{'ip': '10.20.21.81'}]},
            {'id': 2, 'listen': [{'ip': '10.40.96.182'}]},
        ]

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        self.assertIn('truenas_iscsi_portal_id', str(caught.exception))

    def test_several_portals_with_a_choice_is_accepted(self):
        driver = self._driver(truenas_iscsi_portal_id=2)
        driver.client.get_portals.return_value = [
            {'id': 1, 'listen': [{'ip': '10.20.21.81'}]},
            {'id': 2, 'listen': [{'ip': '10.40.96.182'}]},
        ]

        driver.check_for_setup_error()

        self.assertEqual(driver.portal_id, 2)
        self.assertEqual(driver.portal_addresses, ['10.40.96.182'])

    def test_wildcard_portal_without_configured_addresses_is_refused(self):
        # 0.0.0.0 is valid on the appliance and useless to an initiator.
        driver = self._driver()
        driver.client.get_portals.return_value = [
            {'id': 1, 'listen': [{'ip': '0.0.0.0'}]},
        ]

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        message = str(caught.exception)
        self.assertIn('0.0.0.0', message)
        self.assertIn('truenas_iscsi_portal_addresses', message)

    def test_configured_addresses_rescue_a_wildcard_portal(self):
        driver = self._driver(
            truenas_iscsi_portal_addresses=['10.20.21.81'])
        driver.client.get_portals.return_value = [
            {'id': 1, 'listen': [{'ip': '0.0.0.0'}]},
        ]

        driver.check_for_setup_error()

        self.assertEqual(driver.portal_addresses, ['10.20.21.81'])

    def test_configured_addresses_win_over_the_portal(self):
        # The operator knows what is routable; the appliance does not.
        driver = self._driver(
            truenas_iscsi_portal_addresses=['10.99.0.5', '10.99.0.6'])

        driver.check_for_setup_error()

        self.assertEqual(driver.portal_addresses,
                         ['10.99.0.5', '10.99.0.6'])

    def test_rejected_volume_name_template_is_refused(self):
        driver = self._driver(volume_name_template='Volume_%s')
        driver.client.validate_target_name.return_value = (
            'Only lowercase alphanumeric characters plus dot (.), dash '
            '(-), and colon (:) are allowed.')

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        self.assertIn('volume_name_template', str(caught.exception))

    def test_name_template_is_validated_as_a_rendered_sample(self):
        # Sending the raw template would validate a literal '%s'.
        driver = self._driver(volume_name_template='volume-%s')

        driver.check_for_setup_error()

        sent = driver.client.validate_target_name.call_args.args[0]
        self.assertNotIn('%s', sent)
        self.assertTrue(sent.startswith('volume-'))

    def test_template_without_a_placeholder_is_refused(self):
        driver = self._driver(volume_name_template='volume')

        with self.assertRaises(exception.InvalidInput):
            driver.check_for_setup_error()

    def test_template_with_a_stray_percent_is_refused(self):
        # '%' formatting raises ValueError, not TypeError, for an
        # incomplete or unknown conversion. Catching only TypeError let it
        # escape check_for_setup_error as a raw formatting error.
        for template in ('volume-%s-100%', 'volume-%s%', 'volume-%q'):
            driver = self._driver(volume_name_template=template)

            with self.assertRaises(exception.InvalidInput) as caught:
                driver.check_for_setup_error()

            self.assertIn('volume_name_template', str(caught.exception))

    def test_unrenderable_template_is_never_sent_to_the_appliance(self):
        driver = self._driver(volume_name_template='volume-%q')

        with self.assertRaises(exception.InvalidInput):
            driver.check_for_setup_error()

        driver.client.validate_target_name.assert_not_called()


class FakeVolume(object):
    """Minimal stand-in for a Cinder volume object."""

    def __init__(self, name='volume-4d9e1a5c-8f3b-4a21-9c77-2e6b0f1d3a84',
                 size=10):
        self.name = name
        self.size = size


class TestApplianceLockId(DriverTestCase):
    """The lock name component identifying one appliance."""

    def test_the_host_is_used(self):
        self.assertEqual(
            tnd.TrueNASISCSIDriver._appliance_lock_id('https://nas.example'),
            'nas.example')

    def test_a_port_does_not_change_it(self):
        # https://nas/ and https://nas:443/ are the same appliance.
        self.assertEqual(
            tnd.TrueNASISCSIDriver._appliance_lock_id('https://nas:443/'),
            tnd.TrueNASISCSIDriver._appliance_lock_id('https://nas/'))

    def test_characters_unsafe_in_a_path_are_replaced(self):
        # tooz's file driver puts the lock name in a path.
        got = tnd.TrueNASISCSIDriver._appliance_lock_id('https://NAS_one/')

        self.assertEqual(got, 'nas-one')

    def test_a_bare_host_still_yields_something(self):
        self.assertTrue(
            tnd.TrueNASISCSIDriver._appliance_lock_id('nas.example'))

    def test_two_appliances_do_not_share_a_lock(self):
        self.assertNotEqual(
            tnd.TrueNASISCSIDriver._appliance_lock_id('https://a.example'),
            tnd.TrueNASISCSIDriver._appliance_lock_id('https://b.example'))


class TestBackendIdentity(DriverTestCase):
    """Every message an operator acts on names the backend it came from.

    With one backend this is redundant. Cinder multi-backend is routine
    and this migration may span more than one appliance, and then a
    message saying the iSCSI service is stopped *somewhere* costs the
    operator the time it takes to check each one. The traceback does not
    help: it names the driver class, which is identical across every
    instance of it.
    """

    def test_the_backend_is_named_as_the_operator_wrote_it(self):
        # volume_backend_name, not the URL: it is what is in their
        # cinder.conf and what `openstack volume service list` prints.
        self.assertEqual(self._driver().backend_name, 'truenas-iscsi')

    def test_without_a_backend_name_the_host_identifies_it(self):
        driver = self._driver(volume_backend_name=None)

        # The host, not the URL: see the credential test below.
        self.assertEqual(driver.backend_name, 'truenas.example.com')

    def test_the_tag_never_carries_inline_credentials(self):
        """The message that rejects them must not print them (#61).

        `do_setup` refuses a `truenas_api_url` with a userinfo component,
        because requests turns it into a Basic header that overwrites the
        Bearer key and keeps it in `response.url` (#11). Tagging that
        refusal with the raw URL printed the password immediately before
        the sentence explaining that inline credentials leak.

        Asserted on the password rather than on the whole URL, so that
        any future identifier that happens to include the userinfo fails
        here however it is formatted.
        """
        driver = self._driver(
            volume_backend_name=None,
            truenas_api_url='https://admin:sup3rs3cret@truenas.example.com')

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.do_setup(None)

        message = str(caught.exception)
        self.assertNotIn('sup3rs3cret', message)
        self.assertNotIn('admin:', message)
        # Still identifies the appliance, or the tag is worthless.
        self.assertIn('[truenas.example.com]', message)

    def test_a_malformed_url_does_not_replace_the_real_error(self):
        """`backend_name` runs while a failure is being reported.

        Raising here would swap the operator's actual problem for a
        parse error inside the driver's own logging.
        """
        driver = self._driver(volume_backend_name=None,
                              truenas_api_url='https://[not-an-ipv6/')

        self.assertEqual(driver.backend_name, 'TrueNASISCSIDriver')

    def test_with_neither_it_still_names_something(self):
        # Never empty: `[] The iSCSI service is STOPPED` would be worse
        # than no tag at all, because it looks like a bug in the driver
        # rather than a gap in the configuration.
        driver = self._driver(volume_backend_name=None, truenas_api_url=None)

        self.assertEqual(driver.backend_name, 'TrueNASISCSIDriver')

    def test_the_identifier_resolves_when_the_message_is_built(self):
        """Not cached at setup -- setup is what raises most of these.

        `check_for_setup_error` fails before any state a `do_setup` could
        have resolved, so an identifier captured there would be captured
        too late to name the failure setup itself produced.
        """
        driver = self._driver()
        driver.configuration._values['volume_backend_name'] = 'renamed'

        self.assertIn('[renamed]', driver._tagged('anything'))

    def test_a_configuration_error_names_the_backend(self):
        driver = self._driver()
        driver.client.get_pool_list.return_value = [{'name': 'Other-Pool'}]

        with self.assertRaises(exception.InvalidInput) as caught:
            driver.check_for_setup_error()

        self.assertIn('[truenas-iscsi]', str(caught.exception))
        # The tag is an addition, not a replacement: the message still
        # has to be actionable on its own.
        self.assertIn('truenas_pool', str(caught.exception))

    def test_an_appliance_error_names_the_backend(self):
        driver = self._driver()
        driver.client.get_pool_list.side_effect = api_client.TrueNASAPIError(
            'connection refused')

        with self.assertRaises(exception.VolumeBackendAPIException) as caught:
            driver.check_for_setup_error()

        self.assertIn('[truenas-iscsi]', str(caught.exception))

    def test_a_refused_adoption_names_the_backend(self):
        driver = self._driver()

        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing_get_size(FakeVolume('volume-x'), {})

        self.assertIn('[truenas-iscsi]', str(caught.exception))

    def test_a_warning_names_the_backend(self):
        driver = self._driver()
        driver.client.get_iscsi_service.return_value = {
            'service': 'iscsitarget', 'state': 'RUNNING', 'enable': False,
        }

        with mock.patch.object(tnd, 'LOG') as logger:
            driver.check_for_setup_error()

        warned = ' '.join(str(call) for call in logger.warning.call_args_list)
        self.assertIn('[truenas-iscsi]', warned)
        self.assertIn('enabled at boot', warned)


class TestEveryMessageNamesItsBackend(unittest.TestCase):
    """Structural guard, so a message added later cannot skip the tag.

    The tests above assert the tag on the messages that exist today. The
    case worth protecting against is the one added next month, and no
    number of per-message assertions catches that -- they only ever
    cover what someone already remembered to cover.

    So this reads `driver.py` itself and fails on an untagged message,
    which is a thing that can be *written* rather than a thing that must
    be *thought of*.
    """

    # Where an operator reads a log line that names no volume. A
    # lifecycle line always names the volume or snapshot it acted on, and
    # those names are globally unique, so the backend is already
    # recoverable from them. A setup line names only appliance-side
    # objects -- portal 26, pool Dev-Pool -- which are not.
    SETUP_PATH = frozenset({
        'do_setup',
        'check_for_setup_error',
        '_require_reachable_appliance',
        '_require_pool_exists',
        '_require_iscsi_service_running',
        '_resolve_portal',
        '_resolve_portal_addresses',
        '_resolve_iscsi_global',
        '_require_usable_volume_name_template',
    })

    # Exceptions whose message this driver composes, each with the
    # factory that tags it. Cinder composes the rest from keyword
    # arguments -- `VolumeIsBusy(volume_name=...)` and its siblings --
    # and there is no message of ours to prefix.
    FACTORIES = {
        'InvalidInput': '_config_error',
        'VolumeBackendAPIException': '_backend_error',
        'ManageExistingInvalidReference': '_bad_reference',
    }

    def setUp(self):
        import ast

        self.ast = ast
        self.tree = ast.parse(
            pathlib.Path(tnd.__file__).read_text())

    def _is_tagged(self, node):
        return (isinstance(node, self.ast.Call)
                and isinstance(node.func, self.ast.Attribute)
                and node.func.attr == '_tagged'
                and isinstance(node.func.value, self.ast.Name)
                and node.func.value.id == 'self')

    def _functions(self):
        for node in self.ast.walk(self.tree):
            if isinstance(node, self.ast.FunctionDef):
                yield node

    def test_composed_exceptions_are_built_by_their_factory(self):
        """`exception.X(...)` direct is what skips the tag."""
        direct = []
        for function in self._functions():
            if function.name in self.FACTORIES.values():
                continue                      # the factories themselves
            for node in self.ast.walk(function):
                if (isinstance(node, self.ast.Call)
                        and isinstance(node.func, self.ast.Attribute)
                        and isinstance(node.func.value, self.ast.Name)
                        and node.func.value.id == 'exception'
                        and node.func.attr in self.FACTORIES):
                    direct.append('driver.py:%d in %s(): use self.%s()'
                                  % (node.lineno, function.name,
                                     self.FACTORIES[node.func.attr]))

        self.assertEqual(direct, [], 'untagged exception(s):\n  %s'
                         % '\n  '.join(direct))

    def test_operator_facing_log_lines_are_tagged(self):
        untagged = []
        for function in self._functions():
            for node in self.ast.walk(function):
                if not (isinstance(node, self.ast.Call)
                        and isinstance(node.func, self.ast.Attribute)
                        and isinstance(node.func.value, self.ast.Name)
                        and node.func.value.id == 'LOG'
                        and node.args):
                    continue
                operator_facing = (function.name in self.SETUP_PATH
                                   or node.func.attr in ('error', 'warning'))
                if operator_facing and not self._is_tagged(node.args[0]):
                    untagged.append(
                        'driver.py:%d in %s(): LOG.%s needs self._tagged()'
                        % (node.lineno, function.name, node.func.attr))

        self.assertEqual(untagged, [], 'untagged log line(s):\n  %s'
                         % '\n  '.join(untagged))

    def test_the_guard_can_see_the_messages_it_is_guarding(self):
        """A guard that matched nothing would pass for the wrong reason.

        Both tests above are satisfied by an empty search as readily as
        by a clean one, so the counts are asserted separately. If a
        refactor moves these messages somewhere this cannot see, that is
        a finding rather than a pass.
        """
        factory_calls = sum(
            1 for node in self.ast.walk(self.tree)
            if isinstance(node, self.ast.Call)
            and isinstance(node.func, self.ast.Attribute)
            and node.func.attr in self.FACTORIES.values())
        tagged_logs = sum(
            1 for node in self.ast.walk(self.tree)
            if isinstance(node, self.ast.Call)
            and isinstance(node.func, self.ast.Attribute)
            and isinstance(node.func.value, self.ast.Name)
            and node.func.value.id == 'LOG'
            and node.args and self._is_tagged(node.args[0]))

        self.assertGreater(factory_calls, 40)
        self.assertGreater(tagged_logs, 5)

        # And the setup path names real functions. Renaming one would
        # otherwise drop it out of the set silently, which empties the
        # log guard without failing anything.
        defined = {node.name for node in self._functions()}
        self.assertEqual(sorted(self.SETUP_PATH - defined), [])


class TestCreateVolume(DriverTestCase):
    """create_volume."""

    def test_creates_a_zvol_in_the_configured_pool(self):
        driver = self._driver()
        volume = FakeVolume()

        driver.create_volume(volume)

        driver.client.create_zvol.assert_called_once_with(
            POOL, volume.name, volume.size)

    def test_size_is_passed_in_gib(self):
        driver = self._driver()

        driver.create_volume(FakeVolume(size=250))

        self.assertEqual(driver.client.create_zvol.call_args.args[2], 250)

    def test_uses_the_rendered_volume_name(self):
        # volume.name is volume_name_template % name_id, which follows a
        # migrated volume. Using volume.id instead would diverge.
        driver = self._driver()

        driver.create_volume(FakeVolume(name='volume-renamed-id'))

        self.assertEqual(
            driver.client.create_zvol.call_args.args[1], 'volume-renamed-id')

    def test_backend_failure_is_translated(self):
        driver = self._driver()
        driver.client.create_zvol.side_effect = (
            api_client.TrueNASAPIError('pool is full'))

        with self.assertRaises(exception.VolumeBackendAPIException) as caught:
            driver.create_volume(FakeVolume())

        self.assertIn('pool is full', str(caught.exception))

    def test_no_raw_client_exception_escapes(self):
        # The driver must never leak a TrueNASAPIError to Cinder.
        driver = self._driver()
        driver.client.create_zvol.side_effect = (
            api_client.TrueNASAPITimeoutError('timed out'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_volume(FakeVolume())


class TestDeleteVolume(DriverTestCase):
    """delete_volume."""

    def _driver(self, **over):
        driver = super()._driver(**over)
        driver.client.get_snapshot_list.return_value = []
        return driver

    def test_deletes_non_recursively(self):
        # recursive=True would destroy the volume's snapshots with it.
        driver = self._driver()
        volume = FakeVolume()

        driver.delete_volume(volume)

        driver.client.delete_zvol.assert_called_once_with(
            POOL, volume.name, recursive=False)

    def test_already_gone_counts_as_deleted(self):
        driver = self._driver()
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPINotFoundError('no such dataset'))

        driver.delete_volume(FakeVolume())

    def test_already_gone_does_not_go_looking_for_snapshots(self):
        driver = self._driver()
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPINotFoundError('no such dataset'))

        driver.delete_volume(FakeVolume())

        driver.client.get_snapshot_list.assert_not_called()

    def test_snapshots_make_the_volume_busy_not_an_error(self):
        # VolumeIsBusy returns the volume to 'available'; a generic error
        # would strand it in 'error_deleting'.
        driver = self._driver()
        volume = FakeVolume()
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPIError('volume has children'))
        driver.client.get_snapshot_list.return_value = [
            {'id': f'{POOL}/{volume.name}@snap-1', 'snapshot_name': 'snap-1'},
        ]

        with self.assertRaises(exception.VolumeIsBusy):
            driver.delete_volume(volume)

    def test_snapshots_are_checked_on_the_right_dataset(self):
        driver = self._driver()
        volume = FakeVolume()
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPIError('volume has children'))
        driver.client.get_snapshot_list.return_value = [
            {'id': 'x@snap-1', 'snapshot_name': 'snap-1'}]

        with self.assertRaises(exception.VolumeIsBusy):
            driver.delete_volume(volume)

        driver.client.get_snapshot_list.assert_called_once_with(
            dataset=f'{POOL}/{volume.name}')

    def test_failure_without_snapshots_is_a_backend_error(self):
        driver = self._driver()
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPIError('pool is offline'))

        with self.assertRaises(exception.VolumeBackendAPIException) as caught:
            driver.delete_volume(FakeVolume())

        self.assertIn('pool is offline', str(caught.exception))

    def test_snapshot_lookup_failing_reports_the_original_error(self):
        # Asking about snapshots is a courtesy; it must not replace the
        # failure that actually happened.
        driver = self._driver()
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPIError('pool is offline'))
        driver.client.get_snapshot_list.side_effect = (
            api_client.TrueNASAPIConnectionError('gone'))

        with self.assertRaises(exception.VolumeBackendAPIException) as caught:
            driver.delete_volume(FakeVolume())

        self.assertIn('pool is offline', str(caught.exception))

    def test_happy_path_makes_no_snapshot_query(self):
        driver = self._driver()

        driver.delete_volume(FakeVolume())

        driver.client.get_snapshot_list.assert_not_called()


IQN = 'iqn.2005-03.org.open-iscsi:nova-compute-01'
BASENAME = 'iqn.2005-10.org.freenas.ctl'


class ExportTestCase(DriverTestCase):
    """A driver whose setup has already resolved the portal and globals."""

    def _driver(self, addresses=None, **over):
        driver = super()._driver(**over)
        driver.portal_id = 1
        driver.portal_tag = 1
        driver.portal_addresses = addresses or ['10.20.21.81']
        driver.iscsi_basename = BASENAME
        driver.iscsi_port = 3260
        driver.client.get_or_create_initiator_group.return_value = 7
        driver.client.create_extent.return_value = 8
        driver.client.create_target.return_value = 9
        driver.client.create_target_extent.return_value = 10
        driver.client.zvol_disk_path.side_effect = (
            lambda pool, name: f'zvol/{pool}/{name}')
        # Nothing left over from a previous export unless a test says so.
        driver.client.get_extent_by_name.return_value = None
        driver.client.get_target_by_name.return_value = None
        driver.client.get_target_extent.return_value = None
        driver.client.target_groups.side_effect = (
            lambda group, portals: [{'portal': portals, 'initiator': group,
                                     'authmethod': 'NONE'}])
        return driver


class TestCreateExport(ExportTestCase):
    """create_export."""

    def test_builds_the_pipeline_and_reloads(self):
        driver = self._driver()
        volume = FakeVolume()

        driver.create_export(None, volume, {'initiator': IQN})

        driver.client.get_or_create_initiator_group.assert_called_once_with(
            [IQN])
        driver.client.create_extent.assert_called_once_with(
            f'zvol/{POOL}/{volume.name}', volume.name)
        driver.client.create_target.assert_called_once_with(
            volume.name, 7, 1)
        driver.client.create_target_extent.assert_called_once_with(9, 8)
        # Without this the appliance holds the config and nothing attaches.
        driver.client.reload_iscsi_service.assert_called_once_with()

    def test_returns_provider_location_and_id(self):
        driver = self._driver()
        volume = FakeVolume()

        update = driver.create_export(None, volume, {'initiator': IQN})

        self.assertEqual(
            update['provider_location'],
            f'10.20.21.81:3260,1 {BASENAME}:{volume.name} 0')
        self.assertEqual(update['provider_id'], '9:8')

    def test_multipath_joins_addresses_with_semicolons(self):
        # _get_iscsi_properties splits on ';' and only then populates
        # target_portals / target_iqns / target_luns.
        driver = self._driver(addresses=['10.20.21.81', '10.40.96.182'])
        volume = FakeVolume()

        update = driver.create_export(None, volume, {'initiator': IQN})

        self.assertIn('10.20.21.81:3260;10.40.96.182:3260',
                      update['provider_location'])

    def test_address_order_follows_configuration(self):
        driver = self._driver(addresses=['10.40.96.182', '10.20.21.81'])

        update = driver.create_export(None, FakeVolume(),
                                      {'initiator': IQN})

        self.assertTrue(
            update['provider_location'].startswith('10.40.96.182:3260;'))

    def test_missing_initiator_is_refused(self):
        driver = self._driver()

        with self.assertRaises(exception.InvalidConnectorException):
            driver.create_export(None, FakeVolume(), {})

        driver.client.create_extent.assert_not_called()

    def test_no_connector_at_all_is_refused(self):
        driver = self._driver()

        with self.assertRaises(exception.InvalidConnectorException):
            driver.create_export(None, FakeVolume(), None)

    def test_target_failure_rolls_back_the_extent(self):
        driver = self._driver()
        driver.client.create_target.side_effect = (
            api_client.TrueNASAPIError('name taken'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, FakeVolume(), {'initiator': IQN})

        driver.client.best_effort_delete.assert_called_once()
        args = driver.client.best_effort_delete.call_args
        self.assertEqual(args.args[0], driver.client.delete_extent)
        self.assertEqual(args.args[1], 8)

    def test_link_failure_rolls_back_target_then_extent(self):
        driver = self._driver()
        driver.client.create_target_extent.side_effect = (
            api_client.TrueNASAPIError('bad link'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, FakeVolume(), {'initiator': IQN})

        # Reverse order: target first, then the extent it was built on.
        deleted = [c.args[0] for c
                   in driver.client.best_effort_delete.call_args_list]
        self.assertEqual(
            deleted, [driver.client.delete_target,
                      driver.client.delete_extent])

    def test_reload_failure_rolls_back_too(self):
        # Config that never activated is not an export.
        driver = self._driver()
        driver.client.reload_iscsi_service.side_effect = (
            api_client.TrueNASAPIError('service gone'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, FakeVolume(), {'initiator': IQN})

        self.assertEqual(driver.client.best_effort_delete.call_count, 2)

    def test_extent_failure_rolls_back_nothing(self):
        driver = self._driver()
        driver.client.create_extent.side_effect = (
            api_client.TrueNASAPIError('disk in use'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, FakeVolume(), {'initiator': IQN})

        driver.client.best_effort_delete.assert_not_called()


class TestCreateExportIsIdempotent(ExportTestCase):
    """Recovering from an export left behind by a failed attach (#62).

    Cinder does not always call remove_export when an attach fails -- an
    attachment deleted without a connector skips backend cleanup entirely.
    Creating unconditionally then fails with "Extent name must be unique"
    and the volume can never be attached again.
    """

    def test_adopts_an_existing_matching_extent(self):
        driver = self._driver()
        volume = FakeVolume()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'name': volume.name,
            'disk': f'zvol/{POOL}/{volume.name}'}

        update = driver.create_export(None, volume, {'initiator': IQN})

        driver.client.create_extent.assert_not_called()
        self.assertTrue(update['provider_id'].endswith(':27'))

    def test_refuses_an_extent_backed_by_a_different_zvol(self):
        # Adopting this would export another volume's data.
        driver = self._driver()
        volume = FakeVolume()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'name': volume.name,
            'disk': f'zvol/{POOL}/volume-someone-else'}

        with self.assertRaises(exception.VolumeBackendAPIException) as caught:
            driver.create_export(None, volume, {'initiator': IQN})

        message = str(caught.exception)
        self.assertIn('volume-someone-else', message)
        self.assertIn(volume.name, message)

    def test_a_mismatched_extent_is_never_deleted(self):
        # It belongs to something else. Refuse, do not "repair".
        driver = self._driver()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'disk': f'zvol/{POOL}/volume-someone-else'}

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, FakeVolume(), {'initiator': IQN})

        driver.client.delete_extent.assert_not_called()
        driver.client.best_effort_delete.assert_not_called()

    def test_adopts_an_existing_target_and_repoints_it(self):
        # The stale target names the PREVIOUS host's initiator group.
        driver = self._driver()
        volume = FakeVolume()
        driver.client.get_target_by_name.return_value = {
            'id': 27, 'name': volume.name,
            'groups': [{'portal': 1, 'initiator': 999,
                        'authmethod': 'NONE'}]}

        driver.create_export(None, volume, {'initiator': IQN})

        driver.client.create_target.assert_not_called()
        driver.client.update_target_groups.assert_called_once_with(27, 7, 1)

    def test_a_target_already_pointing_the_right_way_is_left_alone(self):
        driver = self._driver()
        driver.client.get_target_by_name.return_value = {
            'id': 27,
            'groups': [{'portal': 1, 'initiator': 7, 'authmethod': 'NONE'}]}

        driver.create_export(None, FakeVolume(), {'initiator': IQN})

        driver.client.update_target_groups.assert_not_called()

    def test_an_existing_link_is_not_recreated(self):
        driver = self._driver()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'disk': f'zvol/{POOL}/{FakeVolume().name}'}
        driver.client.get_target_by_name.return_value = {
            'id': 27,
            'groups': [{'portal': 1, 'initiator': 7, 'authmethod': 'NONE'}]}
        driver.client.get_target_extent.return_value = {'id': 25}

        driver.create_export(None, FakeVolume(), {'initiator': IQN})

        driver.client.create_target_extent.assert_not_called()

    def test_a_missing_link_is_created_even_when_both_ends_exist(self):
        # The orphan from #62 had all three, but a half-built export may
        # have the extent and target without the association.
        driver = self._driver()
        volume = FakeVolume()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'disk': f'zvol/{POOL}/{volume.name}'}
        driver.client.get_target_by_name.return_value = {
            'id': 27,
            'groups': [{'portal': 1, 'initiator': 7, 'authmethod': 'NONE'}]}
        driver.client.get_target_extent.return_value = None

        driver.create_export(None, volume, {'initiator': IQN})

        driver.client.create_target_extent.assert_called_once_with(27, 27)

    def test_adopted_resources_are_not_rolled_back(self):
        # They existed before this call. Deleting them on failure could
        # tear down an export this attach did not create.
        driver = self._driver()
        volume = FakeVolume()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'disk': f'zvol/{POOL}/{volume.name}'}
        driver.client.get_target_by_name.return_value = {
            'id': 27,
            'groups': [{'portal': 1, 'initiator': 7, 'authmethod': 'NONE'}]}
        driver.client.reload_iscsi_service.side_effect = (
            api_client.TrueNASAPIError('service gone'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, volume, {'initiator': IQN})

        driver.client.best_effort_delete.assert_not_called()

    def test_a_freshly_created_extent_is_still_rolled_back(self):
        # The adopt path must not weaken cleanup for what we did create.
        driver = self._driver()
        driver.client.create_target.side_effect = (
            api_client.TrueNASAPIError('nope'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.create_export(None, FakeVolume(), {'initiator': IQN})

        driver.client.best_effort_delete.assert_called_once()

    def test_full_orphan_recovery_creates_nothing_new(self):
        # The exact state observed on the appliance in #62.
        driver = self._driver()
        volume = FakeVolume()
        driver.client.get_extent_by_name.return_value = {
            'id': 27, 'disk': f'zvol/{POOL}/{volume.name}'}
        driver.client.get_target_by_name.return_value = {
            'id': 27,
            'groups': [{'portal': 1, 'initiator': 7, 'authmethod': 'NONE'}]}
        driver.client.get_target_extent.return_value = {'id': 25}

        update = driver.create_export(None, volume, {'initiator': IQN})

        driver.client.create_extent.assert_not_called()
        driver.client.create_target.assert_not_called()
        driver.client.create_target_extent.assert_not_called()
        self.assertEqual(update['provider_id'], '27:27')
        # Still reloaded -- the export must be live even if nothing changed.
        driver.client.reload_iscsi_service.assert_called_once_with()


class TestLocking(ExportTestCase):
    """Serialisation of the two operations that are not concurrency-safe.

    Measured on the appliance in #18: six concurrent
    `get_or_create_initiator_group` calls produced **six** groups. The
    pipeline build, by contrast, ran five ways concurrently without
    failing, so it is deliberately not serialised -- see
    `_reload_exports`.
    """

    def test_the_initiator_group_lookup_takes_a_lock(self):
        driver = self._driver()
        driver.lock_id = 'nas.example.com'

        driver.create_export(None, FakeVolume(), {'initiator': 'iqn.a'})

        self.assertIn('truenas-nas.example.com-initiator',
                      self.lock_names())

    def test_the_reload_takes_a_lock(self):
        driver = self._driver()
        driver.lock_id = 'nas.example.com'

        driver.create_export(None, FakeVolume(), {'initiator': 'iqn.a'})

        self.assertIn('truenas-nas.example.com-reload', self.lock_names())

    def test_the_pipeline_build_itself_is_not_serialised(self):
        # Deliberate. Five concurrent builds were measured succeeding, and
        # serialising them cost 20.2s against 12.5s on exactly the batch
        # attach this matters for. If a pipeline lock is ever added, this
        # test should fail and be reconsidered, not deleted quietly.
        driver = self._driver()
        driver.lock_id = 'nas.example.com'

        driver.create_export(None, FakeVolume(), {'initiator': 'iqn.a'})

        self.assertNotIn('truenas-nas.example.com-pipeline',
                         self.lock_names())

    def test_locks_are_per_appliance_not_global(self):
        # Two backends pointing at different boxes must not serialise
        # against each other: one slow appliance would stall the other.
        first = self._driver()
        first.lock_id = 'nas-one'
        second = self._driver()
        second.lock_id = 'nas-two'

        first.create_export(None, FakeVolume(), {'initiator': 'iqn.a'})
        second.create_export(None, FakeVolume(), {'initiator': 'iqn.a'})

        self.assertIn('truenas-nas-one-initiator', self.lock_names())
        self.assertIn('truenas-nas-two-initiator', self.lock_names())

    def test_remove_export_reload_is_also_serialised(self):
        driver = self._driver()
        driver.lock_id = 'nas.example.com'
        driver.client.get_target_by_name.return_value = {'id': 9}
        driver.client.get_extent_by_name.return_value = {'id': 8}

        driver.remove_export(None, FakeVolume())

        self.assertIn('truenas-nas.example.com-reload', self.lock_names())


class TestRemoveExport(ExportTestCase):
    """remove_export."""

    def _driver(self, **over):
        driver = super()._driver(**over)
        driver.client.get_target_by_name.return_value = {'id': 9}
        driver.client.get_extent_by_name.return_value = {'id': 8}
        return driver

    def test_deletes_target_then_extent_by_name(self):
        driver = self._driver()
        volume = FakeVolume()

        driver.remove_export(None, volume)

        driver.client.get_target_by_name.assert_called_once_with(volume.name)
        driver.client.get_extent_by_name.assert_called_once_with(volume.name)
        driver.client.delete_target.assert_called_once_with(9)
        driver.client.delete_extent.assert_called_once_with(8)
        driver.client.reload_iscsi_service.assert_called_once_with()

    def test_never_uses_provider_id(self):
        # A stale id could address another volume's export.
        driver = self._driver()
        volume = FakeVolume()
        volume.provider_id = '999:999'

        driver.remove_export(None, volume)

        driver.client.delete_target.assert_called_once_with(9)

    def test_nothing_exported_is_not_an_error(self):
        driver = self._driver()
        driver.client.get_target_by_name.return_value = None
        driver.client.get_extent_by_name.return_value = None

        driver.remove_export(None, FakeVolume())

        driver.client.delete_target.assert_not_called()
        driver.client.delete_extent.assert_not_called()
        driver.client.reload_iscsi_service.assert_not_called()

    def test_half_built_export_still_cleans_up(self):
        # create_export failed after the extent but before the target.
        driver = self._driver()
        driver.client.get_target_by_name.return_value = None

        driver.remove_export(None, FakeVolume())

        driver.client.delete_target.assert_not_called()
        driver.client.delete_extent.assert_called_once_with(8)

    def test_already_gone_during_delete_is_tolerated(self):
        driver = self._driver()
        driver.client.delete_target.side_effect = (
            api_client.TrueNASAPINotFoundError('cascaded'))

        driver.remove_export(None, FakeVolume())

        driver.client.delete_extent.assert_called_once_with(8)

    def test_lookup_failure_is_reported(self):
        driver = self._driver()
        driver.client.get_target_by_name.side_effect = (
            api_client.TrueNASAPIConnectionError('unreachable'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.remove_export(None, FakeVolume())

    def test_delete_failure_is_reported(self):
        driver = self._driver()
        driver.client.delete_extent.side_effect = (
            api_client.TrueNASAPIError('busy'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver.remove_export(None, FakeVolume())

    def test_reload_failure_does_not_fail_the_removal(self):
        # The resources are gone; failing here would strand the volume.
        driver = self._driver()
        driver.client.reload_iscsi_service.side_effect = (
            api_client.TrueNASAPIError('service gone'))

        driver.remove_export(None, FakeVolume())


class TestSnapshotOrigin(DriverTestCase):
    """Telling Cinder's snapshots apart from the appliance's."""

    def _busy(self, driver, snapshot_names):
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPIError('volume has children'))
        driver.client.get_snapshot_list.return_value = [
            {'snapshot_name': n} for n in snapshot_names]

    def test_foreign_snapshots_name_the_likely_cause(self):
        driver = self._driver()
        self._busy(driver, ['auto-2026-08-28_00-00'])

        with mock.patch.object(tnd, 'LOG') as log:
            with self.assertRaises(exception.VolumeIsBusy):
                driver.delete_volume(FakeVolume())

        message = log.error.call_args.args[0] % log.error.call_args.args[1]
        self.assertIn('not created by Cinder', message)
        self.assertIn('auto-2026-08-28_00-00', message)

    def test_cinder_snapshots_point_at_delete_ordering(self):
        driver = self._driver()
        self._busy(driver, ['snapshot-1b2c3d4e-0000-0000-0000-000000000000'])

        with mock.patch.object(tnd, 'LOG') as log:
            with self.assertRaises(exception.VolumeIsBusy):
                driver.delete_volume(FakeVolume())

        message = log.error.call_args.args[0] % log.error.call_args.args[1]
        self.assertIn('out of order', message)
        self.assertNotIn('not created by Cinder', message)

    def test_a_single_foreign_snapshot_is_enough_to_flag(self):
        driver = self._driver()
        self._busy(driver, ['snapshot-1b2c3d4e-0000-0000-0000-000000000000',
                            'replication-2026-08-28'])

        with mock.patch.object(tnd, 'LOG') as log:
            with self.assertRaises(exception.VolumeIsBusy):
                driver.delete_volume(FakeVolume())

        message = log.error.call_args.args[0] % log.error.call_args.args[1]
        self.assertIn('not created by Cinder', message)
        self.assertIn('replication-2026-08-28', message)

    def test_custom_snapshot_template_is_honoured(self):
        driver = self._driver(snapshot_name_template='cinder-snap-%s')
        self._busy(driver, ['cinder-snap-abc'])

        with mock.patch.object(tnd, 'LOG') as log:
            with self.assertRaises(exception.VolumeIsBusy):
                driver.delete_volume(FakeVolume())

        message = log.error.call_args.args[0] % log.error.call_args.args[1]
        self.assertIn('out of order', message)

    def test_a_prefixless_template_never_claims_ownership(self):
        # '%s' alone would otherwise make every snapshot look like ours.
        driver = self._driver(snapshot_name_template='%s')

        self.assertFalse(driver._is_cinder_snapshot('anything-at-all'))


class FakeSnapshot(object):
    """Minimal stand-in for a Cinder snapshot object."""

    def __init__(self, name='snapshot-1b2c3d4e-5f60-4718-9a2b-3c4d5e6f7a8b',
                 volume_name=None, size=10):
        self.name = name
        self.volume_name = volume_name or FakeVolume().name
        self.size = size


class SnapshotTestCase(DriverTestCase):
    """A driver with the snapshot paths wired to sensible defaults."""

    def _driver(self, **over):
        driver = super()._driver(**over)
        driver.client.snapshot_id.side_effect = (
            lambda pool, name, snap: f'{pool}/{name}@{snap}')
        driver.client.get_snapshot.return_value = {
            'id': 'x', 'properties': {'clones': {'value': ''}},
        }
        driver.client.get_zvol.return_value = {
            'name': 'z', 'type': 'VOLUME',
            'volsize': {'parsed': 10 * 1024 ** 3},
        }
        return driver

    def _id(self, snapshot):
        return f'{POOL}/{snapshot.volume_name}@{snapshot.name}'


class TestCreateSnapshot(SnapshotTestCase):
    """create_snapshot."""

    def test_snapshots_the_volumes_dataset(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        driver.create_snapshot(snapshot)

        driver.client.create_snapshot.assert_called_once_with(
            f'{POOL}/{snapshot.volume_name}', snapshot.name)

    def test_a_failure_is_reported_as_a_backend_error(self):
        driver = self._driver()
        driver.client.create_snapshot.side_effect = (
            api_client.TrueNASAPIError('refused'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.create_snapshot, FakeSnapshot())


class TestDeleteSnapshot(SnapshotTestCase):
    """delete_snapshot, including the case that must not destroy data."""

    def test_deletes_by_name_derived_id(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        driver.delete_snapshot(snapshot)

        driver.client.delete_snapshot.assert_called_once_with(
            self._id(snapshot))

    def test_never_defers_the_destroy(self):
        # defer=True tells ZFS to destroy the snapshot when its last clone
        # is released: success now, data gone later, outside Cinder's view.
        driver = self._driver()

        driver.delete_snapshot(FakeSnapshot())

        _args, kwargs = driver.client.delete_snapshot.call_args
        self.assertNotIn('defer', kwargs)

    def test_an_already_deleted_snapshot_counts_as_deleted(self):
        driver = self._driver()
        driver.client.delete_snapshot.side_effect = (
            api_client.TrueNASAPINotFoundError('gone'))

        driver.delete_snapshot(FakeSnapshot())

    def test_a_dependent_clone_is_reported_busy_not_failed(self):
        # SnapshotIsBusy returns the snapshot to `available`, which is
        # right: it still exists and is still usable.
        driver = self._driver()
        driver.client.delete_snapshot.side_effect = (
            api_client.TrueNASAPIError('has dependent clones'))
        driver.client.get_snapshot.return_value = {
            'properties': {'clones': {'value': f'{POOL}/some-clone'}},
        }

        self.assertRaises(exception.SnapshotIsBusy,
                          driver.delete_snapshot, FakeSnapshot())

    def test_the_clone_is_named_in_the_log(self):
        driver = self._driver()
        driver.client.delete_snapshot.side_effect = (
            api_client.TrueNASAPIError('nope'))
        driver.client.get_snapshot.return_value = {
            'properties': {'clones': {'value': f'{POOL}/some-clone'}},
        }

        with self.assertLogs(tnd.__name__, level='ERROR') as logged:
            self.assertRaises(exception.SnapshotIsBusy,
                              driver.delete_snapshot, FakeSnapshot())

        self.assertIn('some-clone', '\n'.join(logged.output))

    def test_a_failure_with_no_clones_is_a_backend_error(self):
        driver = self._driver()
        driver.client.delete_snapshot.side_effect = (
            api_client.TrueNASAPIError('disk on fire'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.delete_snapshot, FakeSnapshot())

    def test_an_unreadable_snapshot_falls_back_to_the_original_error(self):
        # Asking what depends on it is a courtesy; if that also fails the
        # original failure is what the operator needs to see.
        driver = self._driver()
        driver.client.delete_snapshot.side_effect = (
            api_client.TrueNASAPIError('original failure'))
        driver.client.get_snapshot.side_effect = (
            api_client.TrueNASAPIError('also broken'))

        with self.assertRaises(exception.VolumeBackendAPIException) as caught:
            driver.delete_snapshot(FakeSnapshot())

        self.assertIn('original failure', str(caught.exception))


class TestManageExistingSnapshot(SnapshotTestCase):
    """Snapshot adoption."""

    def _ref(self, snapshot, source=None):
        return {'source-name':
                source or f'{POOL}/{snapshot.volume_name}@old-snap'}

    def test_renames_the_snapshot_to_the_cinder_name(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        driver.manage_existing_snapshot(snapshot, self._ref(snapshot))

        driver.client.rename_snapshot.assert_called_once_with(
            f'{POOL}/{snapshot.volume_name}@old-snap', snapshot.name)

    def test_adoption_creates_and_deletes_nothing(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        driver.manage_existing_snapshot(snapshot, self._ref(snapshot))

        driver.client.create_snapshot.assert_not_called()
        driver.client.delete_snapshot.assert_not_called()
        driver.client.delete_zvol.assert_not_called()

    def test_a_snapshot_of_another_volume_is_refused(self):
        # The id is derived from snapshot.volume_name, so adopting across
        # volumes would make a record that resolves to nothing and can
        # never be deleted through Cinder.
        driver = self._driver()
        snapshot = FakeSnapshot()

        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing_snapshot(
                snapshot, self._ref(snapshot, f'{POOL}/other-volume@snap'))

        driver.client.rename_snapshot.assert_not_called()
        self.assertIn('other-volume', str(caught.exception))

    def test_a_reference_without_an_at_is_refused(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        self.assertRaises(
            exception.ManageExistingInvalidReference,
            driver.manage_existing_snapshot, snapshot,
            self._ref(snapshot, f'{POOL}/{snapshot.volume_name}'))

    def test_a_reference_with_an_empty_snapshot_name_is_refused(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        self.assertRaises(
            exception.ManageExistingInvalidReference,
            driver.manage_existing_snapshot, snapshot,
            self._ref(snapshot, f'{POOL}/{snapshot.volume_name}@'))

    def test_a_missing_source_name_is_refused(self):
        driver = self._driver()

        self.assertRaises(exception.ManageExistingInvalidReference,
                          driver.manage_existing_snapshot,
                          FakeSnapshot(), {})

    def test_a_reference_that_is_not_a_mapping_is_refused(self):
        driver = self._driver()

        self.assertRaises(exception.ManageExistingInvalidReference,
                          driver.manage_existing_snapshot,
                          FakeSnapshot(), 'Dev-Pool/v@s')

    def test_a_snapshot_that_does_not_exist_is_refused(self):
        driver = self._driver()
        snapshot = FakeSnapshot()
        driver.client.get_snapshot.side_effect = (
            api_client.TrueNASAPINotFoundError('gone'))

        self.assertRaises(exception.ManageExistingInvalidReference,
                          driver.manage_existing_snapshot,
                          snapshot, self._ref(snapshot))

    def test_an_unreadable_appliance_is_a_backend_error(self):
        driver = self._driver()
        snapshot = FakeSnapshot()
        driver.client.get_snapshot.side_effect = (
            api_client.TrueNASAPIError('timeout'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing_snapshot,
                          snapshot, self._ref(snapshot))

    def test_a_failed_rename_is_a_backend_error(self):
        driver = self._driver()
        snapshot = FakeSnapshot()
        driver.client.rename_snapshot.side_effect = (
            api_client.TrueNASAPIError('refused'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing_snapshot,
                          snapshot, self._ref(snapshot))


class TestManageExistingSnapshotGetSize(SnapshotTestCase):
    """Sizing an adopted snapshot."""

    def _ref(self, snapshot):
        return {'source-name': f'{POOL}/{snapshot.volume_name}@old-snap'}

    def test_reports_the_parent_zvols_size(self):
        # A ZFS snapshot has no size of its own; Cinder wants the volume's.
        driver = self._driver()
        snapshot = FakeSnapshot()

        self.assertEqual(
            driver.manage_existing_snapshot_get_size(
                snapshot, self._ref(snapshot)), 10)

    def test_rounds_up(self):
        driver = self._driver()
        snapshot = FakeSnapshot()
        driver.client.get_zvol.return_value = {
            'name': 'z', 'type': 'VOLUME',
            'volsize': {'parsed': int(10.5 * 1024 ** 3)},
        }

        self.assertEqual(
            driver.manage_existing_snapshot_get_size(
                snapshot, self._ref(snapshot)), 11)

    def test_it_reads_the_parent_not_the_snapshot(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        driver.manage_existing_snapshot_get_size(snapshot, self._ref(snapshot))

        driver.client.get_zvol.assert_called_once_with(
            POOL, snapshot.volume_name)

    def test_sizing_does_not_rename_anything(self):
        driver = self._driver()
        snapshot = FakeSnapshot()

        driver.manage_existing_snapshot_get_size(snapshot, self._ref(snapshot))

        driver.client.rename_snapshot.assert_not_called()

    def test_a_bad_reference_is_refused_before_sizing(self):
        driver = self._driver()

        self.assertRaises(exception.ManageExistingInvalidReference,
                          driver.manage_existing_snapshot_get_size,
                          FakeSnapshot(), {})


class TestUnmanageSnapshot(SnapshotTestCase):
    """unmanage_snapshot must release and destroy nothing."""

    def test_it_issues_no_delete(self):
        driver = self._driver()

        driver.unmanage_snapshot(FakeSnapshot())

        driver.client.delete_snapshot.assert_not_called()
        driver.client.delete_zvol.assert_not_called()

    def test_it_does_not_rename_the_snapshot_back(self):
        driver = self._driver()

        driver.unmanage_snapshot(FakeSnapshot())

        driver.client.rename_snapshot.assert_not_called()

    def test_the_only_call_it_makes_is_building_the_id_for_the_log(self):
        # Cinder calls this instead of delete_snapshot, so there is nothing
        # for it to do on the appliance.
        driver = self._driver()

        driver.unmanage_snapshot(FakeSnapshot())

        self.assertEqual(
            [c for c in driver.client.method_calls
             if not c[0].startswith('snapshot_id')], [])


class TestDeleteClonedVolume(SnapshotTestCase):
    """delete_volume reclaims the snapshot a clone was taken from.

    That snapshot has no Cinder object: it never appears in
    `cinder snapshot-list`, so nothing but this can remove it, and left
    behind it blocks the source volume's delete permanently rather than
    while the clone exists.
    """

    def _driver(self, origin=None, **over):
        driver = super()._driver(**over)
        driver.client.get_zvol.return_value = {
            'name': 'z', 'type': 'VOLUME',
            'volsize': {'parsed': 1024 ** 3},
            'origin': {'rawvalue': origin or ''},
        }
        return driver

    def test_a_cloned_volume_reclaims_its_source_snapshot(self):
        volume = FakeVolume()
        snap = f'{POOL}/volume-src@snapshot-clone-src-{volume.name}'
        driver = self._driver(origin=snap)

        driver.delete_volume(volume)

        driver.client.best_effort_delete.assert_called_once()
        self.assertEqual(
            driver.client.best_effort_delete.call_args.args[1], snap)

    def test_a_volume_cloned_from_a_cinder_snapshot_reclaims_nothing(self):
        # Its origin is a real Cinder snapshot with its own lifecycle.
        # Deleting it here would destroy an object Cinder still lists.
        driver = self._driver(
            origin=f'{POOL}/volume-src@snapshot-1b2c3d4e-5f60-4718-9a2b-3c4')

        driver.delete_volume(FakeVolume())

        driver.client.best_effort_delete.assert_not_called()

    def test_a_plain_volume_reclaims_nothing(self):
        driver = self._driver(origin='')

        driver.delete_volume(FakeVolume())

        driver.client.best_effort_delete.assert_not_called()

    def test_the_zvol_is_deleted_before_its_origin(self):
        volume = FakeVolume()
        snap = f'{POOL}/volume-src@snapshot-clone-src-{volume.name}'
        driver = self._driver(origin=snap)
        order = []
        driver.client.delete_zvol.side_effect = (
            lambda *a, **k: order.append('zvol'))
        driver.client.best_effort_delete.side_effect = (
            lambda *a, **k: order.append('origin'))

        driver.delete_volume(volume)

        self.assertEqual(order, ['zvol', 'origin'])

    def test_a_busy_volume_does_not_reclaim_anything(self):
        # The volume still exists, so its clone-source snapshot is still
        # load-bearing.
        volume = FakeVolume()
        driver = self._driver(
            origin=f'{POOL}/volume-src@snapshot-clone-src-{volume.name}')
        driver.client.delete_zvol.side_effect = (
            api_client.TrueNASAPIError('has children'))
        driver.client.get_snapshot_list.return_value = [
            {'snapshot_name': 'snapshot-abc'}]

        self.assertRaises(exception.VolumeIsBusy,
                          driver.delete_volume, volume)

        driver.client.best_effort_delete.assert_not_called()

    def test_an_unreadable_origin_does_not_block_the_delete(self):
        # The lookup is a courtesy; the delete has to proceed regardless.
        driver = self._driver()
        driver.client.get_zvol.side_effect = (
            api_client.TrueNASAPIError('timeout'))

        driver.delete_volume(FakeVolume())

        driver.client.delete_zvol.assert_called_once()
        driver.client.best_effort_delete.assert_not_called()

    def test_reclaiming_uses_best_effort_not_a_raising_delete(self):
        # The volume is already gone; failing here would report a
        # successful delete as a failure and invite a doomed retry.
        volume = FakeVolume()
        driver = self._driver(
            origin=f'{POOL}/volume-src@snapshot-clone-src-{volume.name}')

        driver.delete_volume(volume)

        driver.client.delete_snapshot.assert_not_called()


class TestExtendVolume(SnapshotTestCase):
    """extend_volume."""

    def test_resizes_the_zvol(self):
        driver = self._driver()
        volume = FakeVolume()

        driver.extend_volume(volume, 20)

        driver.client.resize_zvol.assert_called_once_with(
            POOL, volume.name, 20)

    def test_a_failure_is_reported_as_a_backend_error(self):
        driver = self._driver()
        driver.client.resize_zvol.side_effect = (
            api_client.TrueNASAPIError('refused'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.extend_volume, FakeVolume(), 20)

    def test_extending_does_not_touch_snapshots_or_exports(self):
        # ZFS grows a zvol online; nothing needs rebuilding.
        driver = self._driver()

        driver.extend_volume(FakeVolume(), 20)

        driver.client.create_snapshot.assert_not_called()
        driver.client.create_extent.assert_not_called()


class TestCreateVolumeFromSnapshot(SnapshotTestCase):
    """create_volume_from_snapshot."""

    def test_clones_the_snapshot_into_the_new_volume(self):
        driver = self._driver()
        volume = FakeVolume(name='volume-new')
        snapshot = FakeSnapshot()

        driver.create_volume_from_snapshot(volume, snapshot)

        driver.client.clone_snapshot.assert_called_once_with(
            f'{POOL}/{snapshot.volume_name}@{snapshot.name}',
            POOL, 'volume-new')

    def test_it_does_not_promote(self):
        # Promotion reverses the dependency rather than removing it, and
        # moves the snapshot onto the clone -- where this driver could no
        # longer resolve it, since ids come from snapshot.volume_name.
        driver = self._driver()

        driver.create_volume_from_snapshot(FakeVolume(), FakeSnapshot())

        driver.client.promote_clone.assert_not_called()

    def test_it_creates_no_zvol_of_its_own(self):
        # The whole point: the clone shares the snapshot's blocks.
        driver = self._driver()

        driver.create_volume_from_snapshot(FakeVolume(), FakeSnapshot())

        driver.client.create_zvol.assert_not_called()

    def test_a_failed_clone_is_reported_as_a_backend_error(self):
        driver = self._driver()
        driver.client.clone_snapshot.side_effect = (
            api_client.TrueNASAPIError('no such snapshot'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.create_volume_from_snapshot,
                          FakeVolume(), FakeSnapshot())


class TestCreateClonedVolume(SnapshotTestCase):
    """create_cloned_volume."""

    def test_snapshots_the_source_then_clones_that(self):
        driver = self._driver()
        volume = FakeVolume(name='volume-new')
        src = FakeVolume(name='volume-src')
        order = []
        driver.client.create_snapshot.side_effect = (
            lambda *a: order.append('snapshot'))
        driver.client.clone_snapshot.side_effect = (
            lambda *a: order.append('clone'))

        driver.create_cloned_volume(volume, src)

        self.assertEqual(order, ['snapshot', 'clone'])
        driver.client.clone_snapshot.assert_called_once_with(
            f'{POOL}/volume-src@'
            f'{driver._clone_source_snapshot_name(volume)}',
            POOL, 'volume-new')

    def test_the_source_snapshot_is_named_as_cinders(self):
        # It outlives the call and shows up as a blocker on the source's
        # delete. _is_cinder_snapshot must recognise it, or the operator is
        # told something else on the appliance is snapshotting their
        # volumes and goes hunting a task that does not exist.
        driver = self._driver()

        name = driver._clone_source_snapshot_name(FakeVolume())

        self.assertTrue(driver._is_cinder_snapshot(name))

    def test_the_source_snapshot_name_is_unique_per_new_volume(self):
        driver = self._driver()

        a = driver._clone_source_snapshot_name(FakeVolume(name='volume-a'))
        b = driver._clone_source_snapshot_name(FakeVolume(name='volume-b'))

        self.assertNotEqual(a, b)

    def test_it_does_not_promote(self):
        driver = self._driver()

        driver.create_cloned_volume(FakeVolume(), FakeVolume(name='src'))

        driver.client.promote_clone.assert_not_called()

    def test_a_failed_clone_removes_the_snapshot_it_created(self):
        # Leaving it would pin the source against deletion for a copy that
        # does not exist.
        driver = self._driver()
        driver.client.clone_snapshot.side_effect = (
            api_client.TrueNASAPIError('destination exists'))

        volume = FakeVolume()
        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.create_cloned_volume,
                          volume, FakeVolume(name='volume-src'))

        # Which snapshot, not merely that something was deleted.
        driver.client.best_effort_delete.assert_called_once()
        args = driver.client.best_effort_delete.call_args
        self.assertEqual(
            args.args[1],
            f'{POOL}/volume-src@'
            f'{driver._clone_source_snapshot_name(volume)}')

    def test_a_failed_snapshot_does_not_attempt_a_clone(self):
        driver = self._driver()
        driver.client.create_snapshot.side_effect = (
            api_client.TrueNASAPIError('nope'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.create_cloned_volume,
                          FakeVolume(), FakeVolume(name='volume-src'))

        driver.client.clone_snapshot.assert_not_called()
        driver.client.best_effort_delete.assert_not_called()


class AdoptionTestCase(ExportTestCase):
    """A driver ready to adopt, with nothing exporting the target zvol."""

    SOURCE = 'vm-100-disk-0'

    def _driver(self, **over):
        driver = super()._driver(**over)
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}',
            'type': 'VOLUME',
            'volsize': {'parsed': 10 * 1024 ** 3,
                        'rawvalue': str(10 * 1024 ** 3),
                        'value': '10 GiB'},
        }
        driver.client.get_extents.return_value = []
        driver.client.get_target_extents.return_value = []
        driver.client.get_targets.return_value = []
        driver.client.get_iscsi_sessions.return_value = []
        return driver

    def _ref(self, source=None):
        return {'source-name': f'{POOL}/{source or self.SOURCE}'}

    def _export_the_source(self, driver, with_session=False):
        """Point the mocks at a zvol that already has an export."""
        disk = f'zvol/{POOL}/{self.SOURCE}'
        driver.client.get_extents.return_value = [
            {'id': 8, 'name': self.SOURCE, 'disk': disk},
            {'id': 9, 'name': 'unrelated', 'disk': f'zvol/{POOL}/other'},
        ]
        driver.client.get_target_extents.return_value = [
            {'id': 10, 'target': 11, 'extent': 8},
            {'id': 12, 'target': 13, 'extent': 9},
        ]
        driver.client.get_targets.return_value = [
            {'id': 11, 'name': self.SOURCE},
            {'id': 13, 'name': 'unrelated'},
        ]
        if with_session:
            driver.client.get_iscsi_sessions.return_value = [{
                'initiator': 'iqn.2016-04.com.open-iscsi:2a16da8389ad',
                'initiator_addr': '10.20.213.129',
                'target': f'{BASENAME}:{self.SOURCE}',
            }]
        return driver


class TestManageExistingGetSize(AdoptionTestCase):
    """Sizing an adoption candidate."""

    def test_reports_the_zvol_size_in_gb(self):
        driver = self._driver()

        self.assertEqual(
            driver.manage_existing_get_size(FakeVolume(), self._ref()), 10)

    def test_rounds_a_non_aligned_size_up(self):
        # 10.5 GiB must report 11, never 10. Reporting less than the zvol
        # holds would let Cinder believe data fits where it does not.
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'VOLUME',
            'volsize': {'parsed': int(10.5 * 1024 ** 3)},
        }

        self.assertEqual(
            driver.manage_existing_get_size(FakeVolume(), self._ref()), 11)

    def test_rounds_a_single_byte_over_up(self):
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'VOLUME',
            'volsize': {'parsed': 10 * 1024 ** 3 + 1},
        }

        self.assertEqual(
            driver.manage_existing_get_size(FakeVolume(), self._ref()), 11)

    def test_an_exactly_aligned_size_is_not_rounded_up(self):
        # The other half of ceil: exact multiples must not gain a GB.
        driver = self._driver()

        self.assertEqual(
            driver.manage_existing_get_size(FakeVolume(), self._ref()), 10)

    def test_falls_back_to_rawvalue_when_parsed_is_absent(self):
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'VOLUME',
            'volsize': {'rawvalue': str(3 * 1024 ** 3)},
        }

        self.assertEqual(
            driver.manage_existing_get_size(FakeVolume(), self._ref()), 3)

    def test_accepts_a_bare_integer_volsize(self):
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'VOLUME',
            'volsize': 2 * 1024 ** 3,
        }

        self.assertEqual(
            driver.manage_existing_get_size(FakeVolume(), self._ref()), 2)

    def test_an_unreadable_volsize_fails_loudly(self):
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'VOLUME',
            'volsize': {'value': '10 GiB'},
        }

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing_get_size,
                          FakeVolume(), self._ref())

    def test_a_zero_length_zvol_is_refused(self):
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'VOLUME',
            'volsize': {'parsed': 0},
        }

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing_get_size,
                          FakeVolume(), self._ref())

    def test_sizing_does_not_rename_anything(self):
        # Cinder calls this before manage_existing and may never call the
        # second half; sizing must have no side effect.
        driver = self._driver()

        driver.manage_existing_get_size(FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_not_called()


class TestManageExistingReferences(AdoptionTestCase):
    """Every way a reference can be wrong, and what the operator is told."""

    def _assert_invalid(self, driver, ref, *expected):
        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing(FakeVolume(), ref)
        message = str(caught.exception)
        for fragment in expected:
            self.assertIn(fragment, message)
        driver.client.rename_zvol.assert_not_called()
        return message

    def test_missing_source_name(self):
        self._assert_invalid(self._driver(), {}, 'source-name', POOL)

    def test_empty_source_name(self):
        self._assert_invalid(self._driver(), {'source-name': '   '},
                             'source-name')

    def test_source_name_of_the_wrong_type(self):
        self._assert_invalid(self._driver(), {'source-name': 42},
                             'source-name')

    def test_reference_that_is_not_a_mapping(self):
        self._assert_invalid(self._driver(), 'Dev-Pool/vm-100-disk-0',
                             'mapping')

    def test_reference_naming_a_snapshot(self):
        # Diagnosed before the pool check, so naming a snapshot gets the
        # answer that helps rather than a pool complaint.
        self._assert_invalid(
            self._driver(), {'source-name': f'{POOL}/vm-100-disk-0@snap'},
            'snapshot')

    def test_reference_in_another_pool(self):
        self._assert_invalid(
            self._driver(), {'source-name': 'OtherPool/vm-100-disk-0'},
            'OtherPool/vm-100-disk-0', POOL)

    def test_reference_naming_the_pool_itself(self):
        self._assert_invalid(self._driver(), {'source-name': f'{POOL}/'},
                             'zvol')

    def test_reference_to_a_zvol_that_does_not_exist(self):
        driver = self._driver()
        driver.client.get_zvol.side_effect = (
            api_client.TrueNASAPINotFoundError('gone'))

        self._assert_invalid(driver, self._ref(), 'No dataset')

    def test_reference_to_a_filesystem_rather_than_a_zvol(self):
        # A GET on a filesystem answers 200, so this arrives looking like a
        # successful lookup and has to be caught on `type`.
        driver = self._driver()
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{self.SOURCE}', 'type': 'FILESYSTEM',
        }

        self._assert_invalid(driver, self._ref(), 'FILESYSTEM', 'zvol')

    def test_source_name_is_stripped_before_use(self):
        driver = self._driver()

        driver.manage_existing(FakeVolume(),
                               {'source-name': f'  {POOL}/{self.SOURCE}  '})

        driver.client.rename_zvol.assert_called_once_with(
            POOL, self.SOURCE, FakeVolume().name)

    def test_a_nested_source_is_adopted_from_where_it_sits(self):
        # Hand-provisioned disks are often nested under a parent dataset.
        driver = self._driver()
        nested = 'proxmox/vm-100-disk-0'
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{nested}', 'type': 'VOLUME',
            'volsize': {'parsed': 1024 ** 3},
        }

        driver.manage_existing(FakeVolume(), self._ref(nested))

        driver.client.rename_zvol.assert_called_once_with(
            POOL, nested, FakeVolume().name)

    def test_a_failed_lookup_is_a_backend_error_not_a_bad_reference(self):
        # An appliance that cannot be read says nothing about the
        # reference, and telling the operator their reference is wrong
        # would send them looking in the wrong place.
        driver = self._driver()
        driver.client.get_zvol.side_effect = (
            api_client.TrueNASAPIError('appliance on fire'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing, FakeVolume(), self._ref())


class TestManageExisting(AdoptionTestCase):
    """The adoption itself."""

    def test_renames_the_zvol_to_the_cinder_name(self):
        driver = self._driver()
        volume = FakeVolume()

        driver.manage_existing(volume, self._ref())

        driver.client.rename_zvol.assert_called_once_with(
            POOL, self.SOURCE, volume.name)

    def test_adoption_never_creates_or_deletes_a_zvol(self):
        # The whole point: no data is copied and nothing is destroyed.
        driver = self._driver()

        driver.manage_existing(FakeVolume(), self._ref())

        driver.client.create_zvol.assert_not_called()
        driver.client.delete_zvol.assert_not_called()

    def test_returns_no_model_update(self):
        # provider_location is built by create_export at first attach.
        driver = self._driver()

        self.assertIsNone(
            driver.manage_existing(FakeVolume(), self._ref()))

    def test_a_failed_rename_is_reported_as_a_backend_error(self):
        driver = self._driver()
        driver.client.rename_zvol.side_effect = (
            api_client.TrueNASAPIError('refused'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing, FakeVolume(), self._ref())


class TestAdoptionSafetyGate(AdoptionTestCase):
    """Refusing to rename a zvol something else is still exporting.

    The appliance will not do this for us: both rename endpoints demand
    `force` and refuse without it even when idle, so TrueNAS renames
    whatever it is pointed at.
    """

    def test_an_idle_export_is_refused_by_default(self):
        driver = self._export_the_source(self._driver())

        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing(FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_not_called()
        self.assertIn('target 11', str(caught.exception))
        self.assertIn('extent 8', str(caught.exception))

    def test_the_refusal_names_only_the_conflicting_objects(self):
        # An export belonging to a different zvol must not be named, or
        # the operator deletes the wrong thing.
        driver = self._export_the_source(self._driver())

        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing(FakeVolume(), self._ref())

        self.assertNotIn('target 13', str(caught.exception))
        self.assertNotIn('extent 9', str(caught.exception))

    def test_default_refusal_deletes_nothing(self):
        driver = self._export_the_source(self._driver())

        self.assertRaises(exception.ManageExistingInvalidReference,
                          driver.manage_existing, FakeVolume(), self._ref())

        driver.client.delete_target.assert_not_called()
        driver.client.delete_extent.assert_not_called()

    def test_a_live_session_is_refused_even_with_removal_enabled(self):
        # The one case no configuration may authorise: renaming a zvol out
        # from under a connected initiator breaks it mid-write.
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True),
            with_session=True)

        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing(FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_not_called()
        driver.client.delete_target.assert_not_called()
        driver.client.delete_extent.assert_not_called()
        self.assertIn('10.20.213.129', str(caught.exception))

    def test_a_session_on_an_unrelated_target_does_not_block(self):
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))
        driver.client.get_iscsi_sessions.return_value = [{
            'initiator': 'iqn.1994-05.com.redhat:other',
            'initiator_addr': '10.20.213.200',
            'target': f'{BASENAME}:unrelated',
        }]

        driver.manage_existing(FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_called_once()

    def test_removal_when_enabled_takes_the_target_before_the_extent(self):
        # Same order as remove_export: deleting either end cascades the
        # association, and the extent is what pins the zvol.
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))
        order = []
        driver.client.delete_target.side_effect = (
            lambda i: order.append(('target', i)))
        driver.client.delete_extent.side_effect = (
            lambda i: order.append(('extent', i)))

        driver.manage_existing(FakeVolume(), self._ref())

        self.assertEqual(order, [('target', 11), ('extent', 8)])

    def test_removal_leaves_unrelated_exports_alone(self):
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))

        driver.manage_existing(FakeVolume(), self._ref())

        driver.client.delete_target.assert_called_once_with(11)
        driver.client.delete_extent.assert_called_once_with(8)

    def test_removal_never_touches_the_zvol(self):
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))

        driver.manage_existing(FakeVolume(), self._ref())

        driver.client.delete_zvol.assert_not_called()

    def test_the_rename_follows_the_removal(self):
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))
        order = []
        driver.client.delete_extent.side_effect = (
            lambda i: order.append('delete'))
        driver.client.rename_zvol.side_effect = (
            lambda *a: order.append('rename'))

        driver.manage_existing(FakeVolume(), self._ref())

        self.assertEqual(order, ['delete', 'rename'])

    def test_a_failed_removal_stops_before_the_rename(self):
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))
        driver.client.delete_extent.side_effect = (
            api_client.TrueNASAPIError('busy'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing, FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_not_called()

    def test_an_unreadable_appliance_refuses_rather_than_assuming_idle(self):
        # Failing open here would rename a zvol that might be in use.
        driver = self._driver(truenas_adopt_removes_export=True)
        driver.client.get_extents.side_effect = (
            api_client.TrueNASAPIError('timeout'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.manage_existing, FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_not_called()

    def test_an_orphan_extent_with_no_target_still_blocks(self):
        # No target means no session is possible, but the extent still
        # pins the old zvol path and would break on rename.
        driver = self._driver()
        driver.client.get_extents.return_value = [
            {'id': 8, 'name': self.SOURCE, 'disk': f'zvol/{POOL}/'
             f'{self.SOURCE}'},
        ]

        self.assertRaises(exception.ManageExistingInvalidReference,
                          driver.manage_existing, FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_not_called()

    def test_a_nested_zvol_with_a_conflict_names_the_right_export(self):
        """Nested name and export conflict together (#71).

        Both paths derive the zvol's disk path, and a nested name is where
        a path-construction slip would show: `zvol/pool/a/b` rather than
        the flat form every other test uses. It is also the shape a
        hand-provisioned disk most often has.
        """
        driver = self._driver()
        nested = 'proxmox/vm-100-disk-0'
        disk = f'zvol/{POOL}/{nested}'
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{nested}', 'type': 'VOLUME',
            'volsize': {'parsed': 1024 ** 3},
        }
        driver.client.get_extents.return_value = [
            {'id': 21, 'name': 'nested', 'disk': disk},
            {'id': 22, 'name': 'flat', 'disk': f'zvol/{POOL}/vm-100-disk-0'},
        ]
        driver.client.get_target_extents.return_value = [
            {'id': 23, 'target': 24, 'extent': 21},
            {'id': 25, 'target': 26, 'extent': 22},
        ]
        driver.client.get_targets.return_value = [
            {'id': 24, 'name': 'nested'}, {'id': 26, 'name': 'flat'},
        ]

        with self.assertRaises(
                exception.ManageExistingInvalidReference) as caught:
            driver.manage_existing(FakeVolume(), self._ref(nested))

        message = str(caught.exception)
        self.assertIn('target 24', message)
        self.assertIn('extent 21', message)
        # The flat zvol whose name is a suffix of the nested one must not
        # be caught up in it.
        self.assertNotIn('target 26', message)
        self.assertNotIn('extent 22', message)

    def test_a_nested_zvol_conflict_is_cleared_when_the_option_allows(self):
        driver = self._driver(truenas_adopt_removes_export=True)
        nested = 'proxmox/vm-100-disk-0'
        disk = f'zvol/{POOL}/{nested}'
        driver.client.get_zvol.return_value = {
            'name': f'{POOL}/{nested}', 'type': 'VOLUME',
            'volsize': {'parsed': 1024 ** 3},
        }
        driver.client.get_extents.return_value = [
            {'id': 21, 'name': 'nested', 'disk': disk},
            {'id': 22, 'name': 'flat', 'disk': f'zvol/{POOL}/vm-100-disk-0'},
        ]
        driver.client.get_target_extents.return_value = [
            {'id': 23, 'target': 24, 'extent': 21},
            {'id': 25, 'target': 26, 'extent': 22},
        ]
        driver.client.get_targets.return_value = [
            {'id': 24, 'name': 'nested'}, {'id': 26, 'name': 'flat'},
        ]
        volume = FakeVolume()

        driver.manage_existing(volume, self._ref(nested))

        driver.client.delete_target.assert_called_once_with(24)
        driver.client.delete_extent.assert_called_once_with(21)
        driver.client.rename_zvol.assert_called_once_with(
            POOL, nested, volume.name)

    def test_an_unexported_zvol_is_adopted_without_a_session_lookup(self):
        driver = self._driver()

        driver.manage_existing(FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_called_once()


class ManageableTestCase(AdoptionTestCase):
    """A pool holding one adoptable zvol, with the iSCSI lists empty."""

    UUID = '9f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f'

    def _driver(self, **over):
        driver = super()._driver(**over)
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/vm-100-disk-0',
             'volsize': {'parsed': 10 * 1024 ** 3}},
        ]
        driver.client.get_snapshot_list.return_value = []
        return driver

    def _listing(self, driver, managed=()):
        return driver.get_manageable_volumes(
            managed, None, 1000, 0, ['reference'], ['asc'])


class TestManageableVolumes(ManageableTestCase):
    """get_manageable_volumes."""

    def test_an_unexported_zvol_is_reported_safe(self):
        driver = self._driver()

        entry, = self._listing(driver)

        self.assertTrue(entry['safe_to_manage'])
        self.assertIsNone(entry['reason_not_safe'])
        self.assertIsNone(entry['cinder_id'])

    def test_the_reference_is_what_manage_existing_accepts(self):
        # The listing exists to feed adoption. If the reference it hands
        # out is not one manage_existing parses, the feature is decorative.
        driver = self._driver()

        entry, = self._listing(driver)

        self.assertEqual(
            driver._parse_existing_ref(entry['reference']), 'vm-100-disk-0')

    def test_the_size_is_the_zvols_own_rounded_up(self):
        driver = self._driver()
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/vm-100-disk-0',
             'volsize': {'parsed': int(10.5 * 1024 ** 3)}},
        ]

        entry, = self._listing(driver)

        self.assertEqual(entry['size'], 11)

    def test_an_already_managed_zvol_reports_its_cinder_id(self):
        driver = self._driver()
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/volume-{self.UUID}',
             'volsize': {'parsed': 1024 ** 3}},
        ]

        entry, = self._listing(driver, managed=[{'id': self.UUID}])

        self.assertEqual(entry['cinder_id'], self.UUID)
        self.assertFalse(entry['safe_to_manage'])
        self.assertIn('already managed', entry['reason_not_safe'])

    def test_a_zvol_with_a_live_session_is_never_safe(self):
        driver = self._export_the_source(self._driver())
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/{self.SOURCE}',
             'volsize': {'parsed': 1024 ** 3}},
        ]
        driver.client.get_iscsi_sessions.return_value = [{
            'initiator': 'iqn.1994-05.com.redhat:abc',
            'target': f'{BASENAME}:{self.SOURCE}',
        }]

        entry, = self._listing(driver)

        self.assertFalse(entry['safe_to_manage'])
        self.assertIn('in use', entry['reason_not_safe'])
        self.assertIn('iqn.1994-05.com.redhat:abc', entry['reason_not_safe'])

    def test_a_live_session_is_unsafe_even_with_removal_enabled(self):
        # manage_existing refuses this whatever the option says, so
        # reporting it safe would invite an adoption that cannot succeed.
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/{self.SOURCE}',
             'volsize': {'parsed': 1024 ** 3}},
        ]
        driver.client.get_iscsi_sessions.return_value = [{
            'initiator': 'iqn.1994-05.com.redhat:abc',
            'target': f'{BASENAME}:{self.SOURCE}',
        }]

        entry, = self._listing(driver)

        self.assertFalse(entry['safe_to_manage'])

    def test_a_session_on_a_foreign_basename_is_not_our_volume_in_use(self):
        # The basename is appliance-global, so this should not occur in
        # practice -- which is exactly why it is asserted. The comparison
        # is exact, and an inexact one would read a stranger's session as
        # evidence that our zvol is busy and refuse an adoption that is
        # perfectly safe.
        driver = self._export_the_source(self._driver())
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/{self.SOURCE}',
             'volsize': {'parsed': 1024 ** 3}},
        ]
        driver.client.get_iscsi_sessions.return_value = [{
            'initiator': 'iqn.1994-05.com.redhat:abc',
            'target': f'iqn.2001-01.com.example:{self.SOURCE}',
        }]

        entry, = self._listing(driver)

        # Still unsafe -- but for the idle-export reason, not "in use".
        self.assertFalse(entry['safe_to_manage'])
        self.assertNotIn('in use', entry['reason_not_safe'])

    def test_an_idle_export_is_unsafe_by_default_and_names_what_to_remove(
            self):
        driver = self._export_the_source(self._driver())
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/{self.SOURCE}',
             'volsize': {'parsed': 1024 ** 3}},
        ]

        entry, = self._listing(driver)

        self.assertFalse(entry['safe_to_manage'])
        self.assertIn('target 11', entry['reason_not_safe'])
        self.assertIn('extent 8', entry['reason_not_safe'])
        self.assertNotIn('target 13', entry['reason_not_safe'])

    def test_an_idle_export_is_safe_when_the_driver_may_remove_it(self):
        driver = self._export_the_source(
            self._driver(truenas_adopt_removes_export=True))
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/{self.SOURCE}',
             'volsize': {'parsed': 1024 ** 3}},
        ]

        entry, = self._listing(driver)

        self.assertTrue(entry['safe_to_manage'])
        self.assertIn('will be removed', entry['extra_info'])

    def test_the_listing_agrees_with_what_adoption_would_do(self):
        # The listing and the gate must not drift: a volume reported safe
        # is one manage_existing accepts, and vice versa.
        for removes in (False, True):
            with self.subTest(truenas_adopt_removes_export=removes):
                driver = self._export_the_source(
                    self._driver(truenas_adopt_removes_export=removes))
                driver.client.list_zvols.return_value = [
                    {'name': f'{POOL}/{self.SOURCE}',
                     'volsize': {'parsed': 1024 ** 3}},
                ]
                entry, = self._listing(driver)

                if entry['safe_to_manage']:
                    driver.manage_existing(FakeVolume(), entry['reference'])
                else:
                    self.assertRaises(
                        exception.ManageExistingInvalidReference,
                        driver.manage_existing, FakeVolume(),
                        entry['reference'])

    def test_the_iscsi_collections_are_read_once_for_the_whole_listing(self):
        # Not once per zvol (#72). On an estate being migrated this is the
        # difference between four requests and four per disk.
        driver = self._driver()
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/vm-{n}', 'volsize': {'parsed': 1024 ** 3}}
            for n in range(25)
        ]

        self._listing(driver)

        self.assertEqual(driver.client.get_extents.call_count, 1)
        self.assertEqual(driver.client.get_target_extents.call_count, 1)
        self.assertEqual(driver.client.get_targets.call_count, 1)
        self.assertEqual(driver.client.get_iscsi_sessions.call_count, 1)

    def test_an_unreadable_appliance_is_a_backend_error(self):
        driver = self._driver()
        driver.client.list_zvols.side_effect = (
            api_client.TrueNASAPIError('timeout'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          self._listing, driver)

    def test_pagination_is_honoured(self):
        driver = self._driver()
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/vm-{n}', 'volsize': {'parsed': 1024 ** 3}}
            for n in range(10)
        ]

        page = driver.get_manageable_volumes(
            [], None, 3, 0, ['reference'], ['asc'])

        self.assertEqual(len(page), 3)


class TestManageableSnapshots(ManageableTestCase):
    """get_manageable_snapshots."""

    def _snapshots(self, driver, managed=()):
        return driver.get_manageable_snapshots(
            managed, None, 1000, 0, ['reference'], ['asc'])

    def test_a_snapshot_is_listed_against_its_volume(self):
        driver = self._driver()
        driver.client.get_snapshot_list.return_value = [
            {'id': f'{POOL}/vm-100-disk-0@nightly',
             'dataset': f'{POOL}/vm-100-disk-0',
             'snapshot_name': 'nightly'},
        ]

        entry, = self._snapshots(driver)

        self.assertEqual(entry['reference']['source-name'],
                         f'{POOL}/vm-100-disk-0@nightly')
        self.assertEqual(entry['source_reference']['source-name'],
                         'vm-100-disk-0')
        self.assertTrue(entry['safe_to_manage'])

    def test_the_size_reported_is_the_parent_volumes(self):
        # A ZFS snapshot has no size of its own, and Cinder requires a
        # snapshot's size to match its volume's.
        driver = self._driver()
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/vm-100-disk-0',
             'volsize': {'parsed': int(10.5 * 1024 ** 3)}},
        ]
        driver.client.get_snapshot_list.return_value = [
            {'id': f'{POOL}/vm-100-disk-0@nightly',
             'dataset': f'{POOL}/vm-100-disk-0',
             'snapshot_name': 'nightly'},
        ]

        entry, = self._snapshots(driver)

        self.assertEqual(entry['size'], 11)

    def test_an_already_managed_snapshot_reports_its_cinder_id(self):
        driver = self._driver()
        driver.client.get_snapshot_list.return_value = [
            {'id': f'{POOL}/vm-100-disk-0@snapshot-{self.UUID}',
             'dataset': f'{POOL}/vm-100-disk-0',
             'snapshot_name': f'snapshot-{self.UUID}'},
        ]

        entry, = self._snapshots(driver, managed=[{'id': self.UUID}])

        self.assertEqual(entry['cinder_id'], self.UUID)
        self.assertFalse(entry['safe_to_manage'])

    def test_an_unreadable_appliance_is_a_backend_error(self):
        driver = self._driver()
        driver.client.get_snapshot_list.side_effect = (
            api_client.TrueNASAPIError('timeout'))

        self.assertRaises(exception.VolumeBackendAPIException,
                          self._snapshots, driver)

    def test_snapshots_are_read_once_not_once_per_zvol(self):
        # The same N+1 the volume listing avoids. Asking per dataset costs
        # a request per zvol, which is worst on exactly the estates this
        # feature exists for.
        driver = self._driver()
        driver.client.list_zvols.return_value = [
            {'name': f'{POOL}/vm-{n}', 'volsize': {'parsed': 1024 ** 3}}
            for n in range(25)
        ]

        self._snapshots(driver)

        self.assertEqual(driver.client.get_snapshot_list.call_count, 1)

    def test_snapshots_outside_the_pools_zvols_are_excluded(self):
        # The unfiltered read also returns the appliance's own boot-pool
        # snapshots, and snapshots of filesystem datasets in this pool.
        # Neither is adoptable, and listing them as such would send an
        # operator at a reference `manage_existing` refuses.
        driver = self._driver()
        driver.client.get_snapshot_list.return_value = [
            {'id': f'{POOL}/vm-100-disk-0@keep',
             'dataset': f'{POOL}/vm-100-disk-0', 'snapshot_name': 'keep'},
            {'id': 'boot-pool/ROOT@auto-2026',
             'dataset': 'boot-pool/ROOT', 'snapshot_name': 'auto-2026'},
            {'id': f'{POOL}/a-filesystem@nightly',
             'dataset': f'{POOL}/a-filesystem', 'snapshot_name': 'nightly'},
        ]

        entries = self._snapshots(driver)

        self.assertEqual(
            [e['reference']['source-name'] for e in entries],
            [f'{POOL}/vm-100-disk-0@keep'])


class TestUnmanage(AdoptionTestCase):
    """unmanage must release the volume and destroy nothing."""

    def test_unmanage_issues_no_delete_of_any_kind(self):
        # The acceptance criterion on #20. Getting this wrong destroys a
        # production disk that Cinder was only ever borrowing.
        driver = self._driver()

        driver.unmanage(FakeVolume())

        driver.client.delete_zvol.assert_not_called()
        driver.client.delete_extent.assert_not_called()
        driver.client.delete_target.assert_not_called()
        driver.client.delete_target_extent.assert_not_called()
        driver.client.delete_snapshot.assert_not_called()

    def test_unmanage_makes_no_api_call_at_all(self):
        # Cinder calls remove_export() immediately before this and then
        # calls unmanage() *instead of* delete_volume(), so there is
        # nothing left for it to do.
        driver = self._driver()

        driver.unmanage(FakeVolume())

        self.assertEqual(driver.client.method_calls, [])

    def test_unmanage_does_not_rename_the_zvol_back(self):
        # The zvol keeps its Cinder name so it can be adopted again.
        driver = self._driver()

        driver.unmanage(FakeVolume())

        driver.client.rename_zvol.assert_not_called()


class TestVolumeStats(DriverTestCase):
    """Capacity reporting -- without it the backend is unschedulable."""

    def test_reports_real_capacity(self):
        driver = self._driver()

        driver._update_volume_stats()

        pool = driver._stats['pools'][0]
        self.assertAlmostEqual(pool['total_capacity_gb'], 98.5, places=1)
        self.assertAlmostEqual(pool['free_capacity_gb'], 96.5, places=1)

    def test_is_schedulable(self):
        # The inherited default reports free 0 with reserved 100, which the
        # capacity filter rejects, so the backend would take no volumes.
        driver = self._driver()

        driver._update_volume_stats()

        pool = driver._stats['pools'][0]
        self.assertGreater(pool['free_capacity_gb'], 0)
        self.assertLess(pool['reserved_percentage'], 100)

    def test_reports_iscsi_protocol_and_backend_name(self):
        driver = self._driver()

        driver._update_volume_stats()

        self.assertEqual(driver._stats['storage_protocol'], 'iSCSI')
        self.assertEqual(driver._stats['volume_backend_name'],
                         'truenas-iscsi')

    def test_reports_thin_provisioning(self):
        # create_zvol uses sparse=True.
        driver = self._driver()

        driver._update_volume_stats()

        self.assertTrue(
            driver._stats['pools'][0]['thin_provisioning_support'])

    def test_pool_vanishing_is_a_backend_error(self):
        driver = self._driver()
        driver.client.get_pool_list.return_value = [{'name': 'Other'}]

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver._update_volume_stats()

    def test_unreachable_appliance_is_a_backend_error(self):
        driver = self._driver()
        driver.client.get_pool_list.side_effect = (
            api_client.TrueNASAPIError('boom'))

        with self.assertRaises(exception.VolumeBackendAPIException):
            driver._update_volume_stats()


class TestDriverVersion(DriverTestCase):
    """The driver reports the package version to Cinder."""

    def test_version_comes_from_the_package(self):
        # Reported in get_volume_stats as driver_version, and shown by
        # `pip show`. Two literals would drift.
        self.assertEqual(tnd.TrueNASISCSIDriver.VERSION,
                         truenas_cinder_driver.__version__)

    def test_stats_report_that_version(self):
        driver = self._driver()

        driver._update_volume_stats()

        self.assertEqual(driver._stats["driver_version"],
                         truenas_cinder_driver.__version__)


if __name__ == '__main__':
    unittest.main()
