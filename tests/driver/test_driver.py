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

import unittest
from unittest import mock

from cinder import exception

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


if __name__ == '__main__':
    unittest.main()
