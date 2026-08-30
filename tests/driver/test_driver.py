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
