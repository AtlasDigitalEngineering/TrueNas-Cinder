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

        self.assertRaises(exception.VolumeBackendAPIException,
                          driver.create_cloned_volume,
                          FakeVolume(), FakeVolume(name='volume-src'))

        driver.client.best_effort_delete.assert_called_once()

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

    def test_an_unexported_zvol_is_adopted_without_a_session_lookup(self):
        driver = self._driver()

        driver.manage_existing(FakeVolume(), self._ref())

        driver.client.rename_zvol.assert_called_once()


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
