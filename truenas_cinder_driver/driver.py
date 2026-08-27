"""
OpenStack Cinder volume driver for TrueNAS Scale over iSCSI.

This module holds the driver skeleton: configuration, setup validation and
capacity reporting. The volume lifecycle (`create_volume`, `delete_volume`)
and the export/connection path (`create_export`, `remove_export`,
`initialize_connection`) are issue #3 and are inherited as
``NotImplementedError`` until then.

**Base class.** ``SanISCSIDriver`` puts ``ISCSIDriver`` in the MRO, which is
where ``_get_iscsi_properties()`` lives -- the method that turns
``provider_location`` into a connection dict, including the multipath form,
without the driver building it by hand. See :meth:`check_for_setup_error`
for the one piece of ``SanDriver`` this driver deliberately does not use.

**The driver never initialises appliance state.** It does not create a
portal and does not start the iSCSI service. Both are appliance-wide, shared
by every volume and by every ``cinder-volume`` worker, so creating them as a
side effect of provisioning is surprising and races between workers. Instead
setup validates its preconditions and fails with a message naming the
offending value, the option involved, and the remedy.
"""

from oslo_config import cfg
from oslo_log import log as logging

from cinder.common import constants
from cinder import exception
from cinder.i18n import _
from cinder.volume import configuration
from cinder.volume.drivers.san import san

from truenas_cinder_driver import api_client

LOG = logging.getLogger(__name__)

GIB = 1024 ** 3

# Cinder's own default, used when the option is not reachable from this
# process. Only ever used to build a sample name for validation.
DEFAULT_VOLUME_NAME_TEMPLATE = 'volume-%s'

# A UUID-shaped sample for validating the rendered target name. Never used
# as a real volume name.
SAMPLE_VOLUME_ID = 'ffffffff-ffff-ffff-ffff-ffffffffffff'

# Wildcards a portal may bind. Valid on the appliance, useless to a compute
# node, which cannot connect to them.
WILDCARD_ADDRESSES = ('0.0.0.0', '::')

truenas_opts = [
    cfg.StrOpt('truenas_api_url',
               help='Base URL of the TrueNAS Scale appliance, with or '
                    'without the /api/v2.0 suffix, e.g. '
                    'https://truenas.example.com. Required.'),
    cfg.StrOpt('truenas_api_key',
               secret=True,
               help='API key for a TrueNAS service account. Required. '
                    'Marked secret so oslo_config redacts it from logged '
                    'configuration dumps.'),
    cfg.StrOpt('truenas_pool',
               help='ZFS pool on the appliance in which volumes are '
                    'created as zvols. Required.'),
    cfg.IntOpt('truenas_iscsi_portal_id',
               help='ID of the iSCSI portal to export volumes through. '
                    'Only required when the appliance has more than one '
                    'portal; with exactly one it is discovered.'),
    cfg.ListOpt('truenas_iscsi_portal_addresses',
                default=[],
                help='Addresses compute nodes should use to reach the '
                     'iSCSI portal, in preference order. Required when a '
                     'portal is bound to 0.0.0.0 or ::, which are not '
                     'addresses an initiator can connect to. Listing more '
                     'than one advertises multipath.'),
    cfg.BoolOpt('truenas_verify_ssl',
                default=True,
                help='Verify the appliance TLS certificate. Leave enabled: '
                     'fix the certificate rather than disabling this.'),
]

CONF = cfg.CONF
CONF.register_opts(truenas_opts, group=configuration.SHARED_CONF_GROUP)


class TrueNASISCSIDriver(san.SanISCSIDriver):
    """Cinder volume driver for TrueNAS Scale over iSCSI.

    Version history:
        1.0.0 - Configuration, setup validation and capacity reporting.
    """

    VERSION = '1.0.0'

    # Informational. This driver is maintained out of tree and has no
    # third-party CI reporting to OpenStack.
    CI_WIKI_NAME = 'TrueNAS_Cinder_CI'

    def __init__(self, *args, **kwargs):
        """Initialize the driver and register its configuration options."""
        super(TrueNASISCSIDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(truenas_opts)
        self.client = None
        # Resolved during check_for_setup_error, used by the export path.
        self.portal_id = None
        self.portal_addresses = []

    @staticmethod
    def get_driver_options():
        """Return the options this driver adds, for the config generator.

        Returns:
            List of oslo_config options
        """
        return truenas_opts

    def do_setup(self, context):
        """Build the API client from configuration.

        Deliberately does no validation and makes no requests: Cinder calls
        :meth:`check_for_setup_error` immediately afterwards, and keeping
        the two apart means a configuration error is reported by the method
        whose job that is.

        Args:
            context: Security context, unused

        Raises:
            InvalidInput: If a required option is unset
        """
        missing = [
            name for name in ('truenas_api_url', 'truenas_api_key',
                              'truenas_pool')
            if not self.configuration.safe_get(name)
        ]
        if missing:
            raise exception.InvalidInput(
                reason=_('Missing required TrueNAS driver options in '
                         'cinder.conf: %(missing)s. See the sample backend '
                         'section in the driver documentation.')
                % {'missing': ', '.join(sorted(missing))})

        try:
            self.client = api_client.TrueNASAPIClient(
                base_url=self.configuration.truenas_api_url,
                api_key=self.configuration.truenas_api_key,
                verify_ssl=self.configuration.truenas_verify_ssl,
            )
        except ValueError as exc:
            # The client rejects a base_url carrying inline credentials.
            raise exception.InvalidInput(
                reason=_('truenas_api_url is not usable: %s') % exc)

    def check_for_setup_error(self):
        """Verify every precondition the driver depends on.

        **Deliberately does not call super().** ``SanDriver`` requires
        ``san_ip`` plus one of ``san_password`` / ``san_private_key``,
        because it drives an array over SSH. This driver talks to the REST
        API with a Bearer key and never opens an SSH connection, so those
        options are unused -- demanding a password nothing reads would be
        worse than skipping the check. Do not "restore" the super() call.

        Raises:
            InvalidInput: If configuration is wrong in a way the operator
                must fix
            VolumeBackendAPIException: If the appliance is unreachable or
                answers unexpectedly
        """
        pools = self._require_reachable_appliance()
        self._require_pool_exists(pools)
        self._require_iscsi_service_running()
        self._resolve_portal()
        self._require_usable_volume_name_template()
        LOG.info('TrueNAS driver setup validated: pool %(pool)s, portal '
                 '%(portal)s, addresses %(addresses)s.',
                 {'pool': self.configuration.truenas_pool,
                  'portal': self.portal_id,
                  'addresses': self.portal_addresses})

    def _require_reachable_appliance(self):
        """Confirm the appliance answers and the API key is accepted.

        Returns:
            List of pools, reused by the pool check rather than re-fetched

        Raises:
            InvalidInput: If the API key is rejected
            VolumeBackendAPIException: If the appliance cannot be reached
        """
        try:
            return self.client.get_pool_list()
        except api_client.TrueNASAPIAuthError as exc:
            raise exception.InvalidInput(
                reason=_('TrueNAS rejected truenas_api_key. Check that it '
                         'is a valid, unrevoked key for a service account '
                         'with sufficient privileges. %s') % exc)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Cannot reach the TrueNAS appliance at '
                       '%(url)s: %(err)s')
                % {'url': self.configuration.truenas_api_url, 'err': exc})

    def _require_pool_exists(self, pools):
        """Confirm the configured pool is present on the appliance.

        Args:
            pools: Pools as returned by the appliance

        Raises:
            InvalidInput: If the configured pool does not exist
        """
        wanted = self.configuration.truenas_pool
        names = [pool.get('name') for pool in pools]
        if wanted not in names:
            raise exception.InvalidInput(
                reason=_('truenas_pool = %(wanted)r does not exist on the '
                         'appliance. Available pools: %(names)s. Set '
                         'truenas_pool in cinder.conf to one of these.')
                % {'wanted': wanted, 'names': ', '.join(sorted(
                    name for name in names if name)) or 'none'})

    def _require_iscsi_service_running(self):
        """Confirm the appliance's iSCSI service is running.

        The most consequential check here. With the service stopped the
        driver still creates targets and extents successfully and reports
        no error -- a reload does not start it -- and nothing attaches.
        Verified against TrueNAS-25.10.5 (#12).

        Raises:
            InvalidInput: If the service is not running
            VolumeBackendAPIException: If its state cannot be read
        """
        try:
            service = self.client.get_iscsi_service()
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not read the iSCSI service state from the '
                       'TrueNAS appliance: %s') % exc)

        if service.get('state') != 'RUNNING':
            raise exception.InvalidInput(
                reason=_('The iSCSI service on the TrueNAS appliance is '
                         '%(state)s. This driver does not start it. Enable '
                         'it in the TrueNAS UI under System Settings -> '
                         'Services -> iSCSI and set it to start '
                         'automatically, or POST /service/update '
                         '{"service": "iscsitarget", "options": {"enable": '
                         'true}} followed by POST /service/start.')
                % {'state': service.get('state')})

        if not service.get('enable'):
            LOG.warning('The TrueNAS iSCSI service is running but is not '
                        'enabled at boot, so every volume will become '
                        'unreachable after the appliance restarts. Set it '
                        'to start automatically in System Settings -> '
                        'Services -> iSCSI.')

    def _resolve_portal(self):
        """Work out which portal to export through, and at what address.

        Raises:
            InvalidInput: If no portal exists, the configured one does not,
                several exist without a choice being made, or the resolved
                address is one no initiator can connect to
            VolumeBackendAPIException: If portals cannot be listed
        """
        try:
            portals = self.client.get_portals()
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not list iSCSI portals on the TrueNAS '
                       'appliance: %s') % exc)

        if not portals:
            raise exception.InvalidInput(
                reason=_('No iSCSI portal is configured on the TrueNAS '
                         'appliance. This driver does not create one. Add '
                         'a portal in the TrueNAS UI under Shares -> Block '
                         'Shares (iSCSI) -> Portals, bound to a statically '
                         'configured address, then set '
                         'truenas_iscsi_portal_id in cinder.conf if the '
                         'appliance has more than one.'))

        configured = self.configuration.safe_get('truenas_iscsi_portal_id')
        available = [portal.get('id') for portal in portals]
        if configured is not None:
            selected = [portal for portal in portals
                        if portal.get('id') == configured]
            if not selected:
                raise exception.InvalidInput(
                    reason=_('truenas_iscsi_portal_id = %(configured)s does '
                             'not exist on the appliance. Portals present: '
                             '%(available)s.')
                    % {'configured': configured,
                       'available': ', '.join(str(i) for i in available)})
            portal = selected[0]
        elif len(portals) == 1:
            portal = portals[0]
            LOG.info('Using the only iSCSI portal on the appliance, id '
                     '%s.', portal.get('id'))
        else:
            raise exception.InvalidInput(
                reason=_('The appliance has %(count)d iSCSI portals '
                         '(%(available)s) and truenas_iscsi_portal_id is '
                         'not set, so there is no way to know which to '
                         'export through. Set it in cinder.conf.')
                % {'count': len(portals),
                   'available': ', '.join(str(i) for i in available)})

        self.portal_id = portal.get('id')
        self.portal_addresses = self._resolve_portal_addresses(portal)

    def _resolve_portal_addresses(self, portal):
        """Decide which addresses to advertise to compute nodes.

        The configured list wins outright. It is the operator's statement
        of what is actually routable, and on a multi-homed appliance
        nothing else can know that. Listing more than one advertises
        multipath.

        Args:
            portal: The selected portal

        Returns:
            List of addresses, in the order they should be advertised

        Raises:
            InvalidInput: If the portal binds only a wildcard and no
                address has been configured
        """
        configured = self.configuration.safe_get(
            'truenas_iscsi_portal_addresses')
        if configured:
            return list(configured)

        listening = [entry.get('ip') for entry in portal.get('listen', [])]
        usable = [ip for ip in listening if ip not in WILDCARD_ADDRESSES]
        if usable:
            return usable

        raise exception.InvalidInput(
            reason=_('iSCSI portal %(portal)s is bound to %(listening)s, '
                     'which is not an address a compute node can connect '
                     'to. Either rebind the portal to a statically '
                     'configured address, or set '
                     'truenas_iscsi_portal_addresses in cinder.conf to the '
                     'addresses initiators should use.')
            % {'portal': portal.get('id'),
               'listening': ', '.join(ip for ip in listening if ip)
                            or 'nothing'})

    def _require_usable_volume_name_template(self):
        """Confirm rendered volume names are valid iSCSI target names.

        TrueNAS accepts only lowercase alphanumerics plus '.', '-' and ':'.
        Cinder's default `volume-<uuid>` passes, but a deployment that puts
        an underscore or a capital in ``volume_name_template`` does not --
        and would otherwise only discover that at the first attach, long
        after deployment looked successful.

        Raises:
            InvalidInput: If the rendered name would be rejected
            VolumeBackendAPIException: If the name cannot be validated
        """
        template = (self.configuration.safe_get('volume_name_template')
                    or DEFAULT_VOLUME_NAME_TEMPLATE)
        try:
            sample = template % SAMPLE_VOLUME_ID
        except TypeError:
            raise exception.InvalidInput(
                reason=_('volume_name_template = %r does not contain a '
                         'single %%s placeholder for the volume id.')
                % template)

        try:
            reason = self.client.validate_target_name(sample)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not validate the volume name template '
                       'against the appliance: %s') % exc)

        if reason:
            raise exception.InvalidInput(
                reason=_('volume_name_template = %(template)r produces '
                         'iSCSI target names the appliance rejects: '
                         '%(reason)s Set volume_name_template in '
                         'cinder.conf to a lowercase form such as '
                         '%(suggestion)r.')
                % {'template': template, 'reason': reason,
                   'suggestion': DEFAULT_VOLUME_NAME_TEMPLATE})

    def _update_volume_stats(self):
        """Report real capacity so the scheduler will place volumes here.

        **Not optional.** The inherited implementation reports
        ``free_capacity_gb=0`` with ``reserved_percentage=100``, which the
        scheduler's capacity filter rejects -- the backend would accept no
        volumes at all and nothing would say why.

        Raises:
            VolumeBackendAPIException: If capacity cannot be read
        """
        pool_name = self.configuration.truenas_pool
        backend_name = (self.configuration.safe_get('volume_backend_name')
                        or self.__class__.__name__)

        try:
            pools = self.client.get_pool_list()
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not read pool capacity from the TrueNAS '
                       'appliance: %s') % exc)

        pool = next((entry for entry in pools
                     if entry.get('name') == pool_name), None)
        if pool is None:
            raise exception.VolumeBackendAPIException(
                data=_('Pool %(pool)r has disappeared from the appliance. '
                       'It existed when the driver started.')
                % {'pool': pool_name})

        # `size` and `free` are bytes; `allocated` is reported too but free
        # is authoritative because it accounts for reservations.
        total_gb = int(pool.get('size') or 0) / GIB
        free_gb = int(pool.get('free') or 0) / GIB

        self._stats = {
            'volume_backend_name': backend_name,
            'vendor_name': 'iXsystems',
            'driver_version': self.VERSION,
            'storage_protocol': constants.ISCSI,
            'replication_enabled': False,
            'pools': [{
                'pool_name': backend_name,
                'total_capacity_gb': total_gb,
                'free_capacity_gb': free_gb,
                'reserved_percentage':
                    self.configuration.safe_get('reserved_percentage') or 0,
                'thin_provisioning_support': True,
                'thick_provisioning_support': False,
                'max_over_subscription_ratio':
                    self.configuration.safe_get(
                        'max_over_subscription_ratio'),
                'QoS_support': False,
                'multiattach': False,
                'filter_function': self.get_filter_function(),
                'goodness_function': self.get_goodness_function(),
            }],
        }
