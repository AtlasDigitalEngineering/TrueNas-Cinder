"""
OpenStack Cinder volume driver for TrueNAS Scale over iSCSI.

Configuration and setup validation, capacity reporting, the volume
lifecycle, the iSCSI export path, and adoption of zvols the driver did not
create.

Setup validates the appliance and never changes it: it does not create a
portal or start the iSCSI service. See AGENTS.md for the reasoning.
"""

from oslo_config import cfg
from oslo_log import log as logging

from cinder.common import constants
from cinder import exception
from cinder.i18n import _
from cinder.volume import configuration
from cinder.volume.drivers.san import san

from truenas_cinder_driver import __version__
from truenas_cinder_driver import api_client

LOG = logging.getLogger(__name__)

GIB = 1024 ** 3

# Cinder's default, for when the option is not registered in this process.
DEFAULT_VOLUME_NAME_TEMPLATE = 'volume-%s'
SAMPLE_VOLUME_ID = 'ffffffff-ffff-ffff-ffff-ffffffffffff'

DEFAULT_SNAPSHOT_NAME_TEMPLATE = 'snapshot-%s'

# One LUN per target: each volume gets its own target.
LUN_ID = 0

# Accepted by the appliance, unreachable from a compute node.
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
    cfg.BoolOpt('truenas_adopt_removes_export',
                default=False,
                help='Allow manage_existing to delete an iSCSI export that '
                     'already exists on the zvol being adopted. Off by '
                     'default, in which case the driver refuses and names '
                     'the target and extent to remove. Turn it on to '
                     'migrate hand-provisioned disks without a manual '
                     'cleanup step per disk. A zvol with a live iSCSI '
                     'session is refused either way, whatever this is set '
                     'to.'),
]

CONF = cfg.CONF
CONF.register_opts(truenas_opts, group=configuration.SHARED_CONF_GROUP)


class TrueNASISCSIDriver(san.SanISCSIDriver):
    """Cinder volume driver for TrueNAS Scale over iSCSI.

    Version history:
        1.0.0 - Setup validation, capacity reporting, volume lifecycle,
                iSCSI export, and adoption of existing zvols.
    """

    # From the package, so `pip show` and get_volume_stats agree.
    VERSION = __version__

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
        self.portal_tag = None
        self.portal_addresses = []
        self.iscsi_basename = None
        self.iscsi_port = None

    @staticmethod
    def get_driver_options():
        """Return the options this driver adds, for the config generator.

        Returns:
            List of oslo_config options
        """
        return truenas_opts

    def do_setup(self, context):
        """Build the API client from configuration.

        Checks the required options are set -- constructing the client
        without them fails less clearly. Anything that has to ask the
        appliance a question belongs to :meth:`check_for_setup_error`.

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

        **Deliberately does not call super().** ``SanDriver`` demands
        ``san_ip`` and an SSH password or key, none of which this driver
        uses. ``test_does_not_require_ssh_configuration`` fails if the
        call is restored.

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
        self._resolve_iscsi_global()
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

        The check that matters most: with the service stopped the
        appliance still accepts target and extent configuration, reports no
        error, and nothing attaches (#12).

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
        self.portal_tag = portal.get('tag')
        self.portal_addresses = self._resolve_portal_addresses(portal)

    def _resolve_portal_addresses(self, portal):
        """Decide which addresses to advertise to compute nodes.

        The configured list wins: on a multi-homed appliance only the
        operator knows which addresses are routable. Several of them
        advertise multipath.

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

    def _resolve_iscsi_global(self):
        """Read the IQN prefix and port the appliance serves iSCSI on.

        Cached rather than fetched per export: both are appliance-wide and
        change about as often as the hostname.

        Raises:
            VolumeBackendAPIException: If the global config cannot be read,
                or carries no ``basename`` to build target IQNs from
        """
        try:
            config = self.client.get_iscsi_global_config()
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not read the iSCSI configuration from the '
                       'TrueNAS appliance: %s') % exc)

        self.iscsi_basename = config.get('basename')
        self.iscsi_port = config.get('listen_port')
        if not self.iscsi_basename:
            raise exception.VolumeBackendAPIException(
                data=_('The TrueNAS appliance reported no iSCSI basename, '
                       'so target IQNs cannot be built.'))

    def _require_usable_volume_name_template(self):
        """Confirm rendered volume names are valid iSCSI target names.

        TrueNAS accepts only lowercase alphanumerics plus '.', '-' and
        ':'. Checked here rather than at the first attach, which is where
        it would otherwise surface.

        Raises:
            InvalidInput: If the template cannot be rendered at all, or the
                name it produces would be rejected by the appliance
            VolumeBackendAPIException: If the name cannot be validated
        """
        template = (self.configuration.safe_get('volume_name_template')
                    or DEFAULT_VOLUME_NAME_TEMPLATE)
        try:
            sample = template % SAMPLE_VOLUME_ID
        except (TypeError, ValueError) as exc:
            # TypeError covers the wrong number of placeholders; ValueError
            # covers a stray percent sign ('volume-%s-100%' -> "incomplete
            # format") or an unknown conversion ('%q'). Both are plausible
            # typos, and neither should escape as a raw formatting error.
            raise exception.InvalidInput(
                reason=_('volume_name_template = %(template)r cannot be '
                         'rendered with a volume id: %(err)s. It needs '
                         'exactly one %%s placeholder, and any literal '
                         'percent sign must be written as %%%%.')
                % {'template': template, 'err': exc})

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

    def ensure_export(self, context, volume):
        """Nothing to do -- exports live on the appliance, not in memory.

        Called when the volume service restarts. Targets, extents and their
        association are appliance state and survive a restart, and teardown
        re-derives them by name rather than from anything cached, so there
        is nothing to rebuild.

        Args:
            context: Security context, unused
            volume: The Cinder volume, unused
        """

    def create_export(self, context, volume, connector):
        """Export the volume over iSCSI and return what Cinder must persist.

        Builds extent -> target -> association, then reloads the service so
        the configuration is actually live. Each step rolls back the ones
        before it, so a failure leaves nothing behind for the operator to
        find later.

        Args:
            context: Security context, unused
            volume: The Cinder volume to export
            connector: Connector dict; its ``initiator`` decides which IQN
                is allowed to attach

        Returns:
            Model update carrying ``provider_location`` and ``provider_id``

        Raises:
            InvalidConnectorException: If the connector carries no initiator
            VolumeBackendAPIException: If the appliance refused any step
        """
        initiator = (connector or {}).get('initiator')
        if not initiator:
            raise exception.InvalidConnectorException(missing='initiator')

        pool = self.configuration.truenas_pool
        name = volume.name
        rollback = []

        try:
            group_id = self.client.get_or_create_initiator_group([initiator])
            extent_id = self._ensure_extent(pool, name, rollback)
            target_id = self._ensure_target(name, group_id, rollback)
            self._ensure_target_extent(target_id, extent_id)

            # Until the service reloads, everything above is inert: the
            # appliance accepts the configuration and no initiator can see
            # it.
            self.client.reload_iscsi_service()
        except api_client.TrueNASAPIError as exc:
            for what, delete, resource_id in reversed(rollback):
                self.client.best_effort_delete(delete, resource_id, what=what)
            raise exception.VolumeBackendAPIException(
                data=_('Could not export volume %(name)s over iSCSI: '
                       '%(err)s') % {'name': name, 'err': exc})

        location = self._provider_location(name)
        LOG.info('Exported %(name)s as %(location)s.',
                 {'name': name, 'location': location})
        return {
            'provider_location': location,
            # A hint for orphan reconciliation and #20, never trusted for
            # teardown -- see remove_export.
            'provider_id': '%s:%s' % (target_id, extent_id),
        }

    def _ensure_extent(self, pool, name, rollback):
        """Return the volume's extent, creating it only if absent.

        An earlier attach that failed after the export was built leaves the
        extent behind, and Cinder does not always call `remove_export` to
        clean it up (#62). Creating unconditionally then fails with "Extent
        name must be unique" and the volume can never be attached again.

        **A name match is not enough to adopt one.** If an extent of this
        name is backed by a different zvol, adopting it would export another
        volume's data into the instance. That is refused rather than
        repaired, because there is no safe automatic answer.

        Args:
            pool: Pool the zvol lives in
            name: Volume name, used for both the zvol and the extent
            rollback: Cleanup stack; only appended to for what is created
                here, never for something adopted

        Returns:
            Id of the extent backing this volume

        Raises:
            VolumeBackendAPIException: If an extent of this name exists but
                is backed by a different disk
        """
        disk = self.client.zvol_disk_path(pool, name)
        existing = self.client.get_extent_by_name(name)

        if existing is None:
            extent_id = self.client.create_extent(disk, name)
            rollback.append(('iSCSI extent %s' % extent_id,
                             self.client.delete_extent, extent_id))
            return extent_id

        if existing.get('disk') != disk:
            raise exception.VolumeBackendAPIException(
                data=_('An iSCSI extent named %(name)s already exists on '
                       'the appliance but is backed by %(actual)s, not '
                       '%(expected)s. Refusing to export it: it belongs to '
                       'something else. Remove or rename it on the '
                       'appliance before attaching this volume.')
                % {'name': name, 'actual': existing.get('disk'),
                   'expected': disk})

        LOG.info('Reusing existing iSCSI extent %(id)s for %(name)s; it was '
                 'left over from an earlier export.',
                 {'id': existing['id'], 'name': name})
        return existing['id']

    def _ensure_target(self, name, group_id, rollback):
        """Return the volume's target, creating it only if absent.

        An adopted target carries the initiator group from whichever host
        attached it last. Re-attaching elsewhere would otherwise appear to
        succeed while the new initiator is refused by an access list naming
        the old one, so the groups are repointed rather than accepted.

        Args:
            name: Volume name, used as the target name
            group_id: Initiator group the attaching host belongs to
            rollback: Cleanup stack; only appended to for what is created
                here

        Returns:
            Id of the target for this volume
        """
        existing = self.client.get_target_by_name(name)

        if existing is None:
            target_id = self.client.create_target(name, group_id,
                                                  self.portal_id)
            rollback.append(('iSCSI target %s' % target_id,
                             self.client.delete_target, target_id))
            return target_id

        wanted = self.client.target_groups(group_id, self.portal_id)
        if existing.get('groups') != wanted:
            LOG.info('Repointing iSCSI target %(id)s at initiator group '
                     '%(group)s and portal %(portal)s.',
                     {'id': existing['id'], 'group': group_id,
                      'portal': self.portal_id})
            self.client.update_target_groups(existing['id'], group_id,
                                             self.portal_id)
        else:
            LOG.info('Reusing existing iSCSI target %(id)s for %(name)s.',
                     {'id': existing['id'], 'name': name})
        return existing['id']

    def _ensure_target_extent(self, target_id, extent_id):
        """Link the target to the extent unless they are already linked.

        Args:
            target_id: Target id
            extent_id: Extent id
        """
        if self.client.get_target_extent(target_id, extent_id) is None:
            self.client.create_target_extent(target_id, extent_id)

    def _provider_location(self, name):
        """Build the iSCSI discovery string Cinder stores and parses back.

        ``"<ip>:<port>[;<ip>:<port>...],<tag> <IQN> <lun>"``. The inherited
        ``_get_iscsi_properties()`` splits the address list on ``;`` and
        fills ``target_portals`` / ``target_iqns`` / ``target_luns`` when
        there is more than one, so listing every address is all multipath
        needs from this driver.

        Address order comes from configuration, never from the appliance:
        the first entry becomes the singular ``target_portal`` a
        non-multipath connector uses, and TrueNAS does not preserve order.

        Args:
            name: Volume name, which is also the target name

        Returns:
            The provider_location string
        """
        portals = ';'.join('%s:%s' % (address, self.iscsi_port)
                           for address in self.portal_addresses)
        return '%s,%s %s:%s %s' % (portals, self.portal_tag,
                                   self.iscsi_basename, name, LUN_ID)

    def remove_export(self, context, volume):
        """Tear down the volume's iSCSI export.

        Finds the target and extent **by name** rather than from
        ``provider_id``: TrueNAS ids are small integers, so a stale one
        could address another volume's export, and checking that a cached
        id still matches costs the same request as looking it up.

        Idempotent, because Cinder also calls this to clean up after a
        failed export and after a failed model update -- so it must cope
        with a half-built export, or none at all.

        Args:
            context: Security context, unused
            volume: The Cinder volume to unexport

        Raises:
            VolumeBackendAPIException: If something exists and could not be
                removed
        """
        name = volume.name
        removed = []

        # Target first: deleting either end cascades the association, and
        # the extent is what holds the zvol.
        for what, find, delete in (
            ('target', self.client.get_target_by_name,
             self.client.delete_target),
            ('extent', self.client.get_extent_by_name,
             self.client.delete_extent),
        ):
            try:
                found = find(name)
            except api_client.TrueNASAPIError as exc:
                raise exception.VolumeBackendAPIException(
                    data=_('Could not look up the iSCSI %(what)s for volume '
                           '%(name)s: %(err)s')
                    % {'what': what, 'name': name, 'err': exc})
            if not found:
                continue
            try:
                delete(found['id'])
            except api_client.TrueNASAPINotFoundError:
                pass
            except api_client.TrueNASAPIError as exc:
                raise exception.VolumeBackendAPIException(
                    data=_('Could not remove the iSCSI %(what)s for volume '
                           '%(name)s: %(err)s')
                    % {'what': what, 'name': name, 'err': exc})
            removed.append('%s %s' % (what, found['id']))

        if not removed:
            LOG.info('Volume %s had no iSCSI export to remove.', name)
            return

        try:
            self.client.reload_iscsi_service()
        except api_client.TrueNASAPIError as exc:
            LOG.warning('Removed %(removed)s for volume %(name)s but could '
                        'not reload the iSCSI service: %(err)s',
                        {'removed': ', '.join(removed), 'name': name,
                         'err': exc})
        LOG.info('Removed %(removed)s for volume %(name)s.',
                 {'removed': ', '.join(removed), 'name': name})

    def create_volume(self, volume):
        """Create the zvol backing a Cinder volume.

        Created sparse, which is what lets the driver report
        ``thin_provisioning_support``. ``volume.name`` renders
        ``volume_name_template`` against the volume's ``name_id``, so it
        follows a migrated volume and matches the name
        :meth:`check_for_setup_error` validated against the appliance.

        Args:
            volume: The Cinder volume to create

        Raises:
            VolumeBackendAPIException: If the appliance refused the create
        """
        pool = self.configuration.truenas_pool
        try:
            self.client.create_zvol(pool, volume.name, volume.size)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not create volume %(name)s in TrueNAS pool '
                       '%(pool)s: %(err)s')
                % {'name': volume.name, 'pool': pool, 'err': exc})

        LOG.info('Created zvol %(pool)s/%(name)s, %(size)s GiB, sparse.',
                 {'pool': pool, 'name': volume.name, 'size': volume.size})

    def delete_volume(self, volume):
        """Delete the zvol backing a Cinder volume.

        Idempotent: a zvol that is already gone counts as deleted, which is
        what lets a retried or half-completed delete converge instead of
        wedging the volume in ``error_deleting``.

        **Never recursive.** ZFS refuses to destroy a zvol that still has
        snapshots, and ``recursive=True`` would destroy them along with it.
        Cinder deletes snapshots before volumes itself, so reaching that
        state means something is already wrong -- and silently destroying
        snapshots to get past it turns a visible failure into data loss
        found at restore time.

        Args:
            volume: The Cinder volume to delete

        Raises:
            VolumeIsBusy: If snapshots still depend on the zvol
            VolumeBackendAPIException: If the delete failed for any other
                reason
        """
        pool = self.configuration.truenas_pool
        # Read before the zvol goes away: after the delete there is nothing
        # left to ask. A clone-source snapshot has no Cinder object, so if
        # it is not reclaimed here nothing ever reclaims it -- and it goes
        # on blocking the source volume's own delete forever.
        origin = self._reclaimable_clone_source(pool, volume)
        try:
            self.client.delete_zvol(pool, volume.name, recursive=False)
        except api_client.TrueNASAPINotFoundError:
            LOG.info('Zvol %(pool)s/%(name)s was already gone; treating the '
                     'delete as complete.',
                     {'pool': pool, 'name': volume.name})
        except api_client.TrueNASAPIError as exc:
            self._raise_delete_failure(volume, pool, exc)
        else:
            LOG.info('Deleted zvol %(pool)s/%(name)s.',
                     {'pool': pool, 'name': volume.name})

        if origin:
            # Best effort: the volume is already gone, so failing here
            # would report a successful delete as a failure and invite a
            # retry that cannot succeed.
            LOG.info('Reclaiming the clone-source snapshot %s, which existed '
                     'only to back the volume just deleted.', origin)
            self.client.best_effort_delete(
                self.client.delete_snapshot, origin,
                what=f'clone-source snapshot {origin}')

    def _raise_delete_failure(self, volume, pool, exc):
        """Re-raise a failed zvol delete, naming snapshots when they caused it.

        The appliance reports the snapshot case as a flat
        ``{"message": ..., "errno": 14}`` body. Rather than key off that --
        message text is unreliable and the errno is undocumented -- this
        asks the appliance directly whether snapshots exist, which costs a
        request only on the failure path and produces a message naming
        them.

        A common cause is snapshots Cinder does not know about: a TrueNAS
        periodic snapshot task covering the pool will block every delete.

        Args:
            volume: The Cinder volume whose delete failed
            pool: Pool the zvol lives in
            exc: The error the delete raised

        Raises:
            VolumeIsBusy: If snapshots still depend on the zvol. The manager
                returns the volume to ``available`` rather than
                ``error_deleting``, which is correct -- it still exists and
                is still usable.
            VolumeBackendAPIException: For any other failure
        """
        dataset = f'{pool}/{volume.name}'
        try:
            snapshots = self.client.get_snapshot_list(dataset=dataset)
        except api_client.TrueNASAPIError:
            # Asking was a courtesy. Report the original failure.
            snapshots = []

        if snapshots:
            self._log_blocking_snapshots(dataset, snapshots)
            raise exception.VolumeIsBusy(volume_name=volume.name)

        raise exception.VolumeBackendAPIException(
            data=_('Could not delete volume %(name)s from TrueNAS pool '
                   '%(pool)s: %(err)s')
            % {'name': volume.name, 'pool': pool, 'err': exc})

    def _log_blocking_snapshots(self, dataset, snapshots):
        """Say which snapshots blocked a delete, and who created them.

        The two cases need different action and are otherwise
        indistinguishable to an operator. Snapshots Cinder created mean its
        own delete ordering went wrong, which is a bug here. Snapshots it
        did not create mean something else on the appliance is snapshotting
        a Cinder-managed zvol -- a periodic snapshot or replication task --
        and no amount of retrying will clear it.

        Args:
            dataset: Full dataset path, for the message
            snapshots: Snapshots returned for that dataset
        """
        names = sorted(snap.get('snapshot_name') or snap.get('id') or '?'
                       for snap in snapshots)
        foreign = [name for name in names
                   if not self._is_cinder_snapshot(name)]

        if foreign:
            LOG.error(
                'Cannot delete %(dataset)s: %(count)d snapshot(s) still '
                'depend on it, and %(n)d of them were not created by Cinder '
                '(%(foreign)s). Something else on the appliance is '
                'snapshotting a Cinder-managed volume -- check for a '
                'periodic snapshot or replication task covering this pool. '
                'The driver will not delete snapshots it does not own.',
                {'dataset': dataset, 'count': len(names), 'n': len(foreign),
                 'foreign': ', '.join(foreign)})
        else:
            LOG.error(
                'Cannot delete %(dataset)s: %(count)d Cinder snapshot(s) '
                'still exist (%(names)s). Cinder deletes snapshots before '
                'volumes, so reaching this state means the volume was '
                'deleted out of order.',
                {'dataset': dataset, 'count': len(names),
                 'names': ', '.join(names)})

    def _is_cinder_snapshot(self, snapshot_name):
        """Decide whether a snapshot name looks like one Cinder created.

        Matches the literal prefix of ``snapshot_name_template`` -- the
        default ``snapshot-%s`` gives ``snapshot-``. A template with no
        prefix makes every snapshot look like Cinder's, so that is treated
        as "cannot tell" rather than "all ours".

        Args:
            snapshot_name: Bare snapshot name, without the dataset or ``@``

        Returns:
            True if the name carries the template's prefix
        """
        template = (self.configuration.safe_get('snapshot_name_template')
                    or DEFAULT_SNAPSHOT_NAME_TEMPLATE)
        prefix = template.split('%s')[0]
        return bool(prefix) and snapshot_name.startswith(prefix)

    # ------------------------------------------------------------------
    # Snapshots
    #
    # A ZFS snapshot is a point-in-time reference to the blocks a zvol held
    # when it was taken, so creating one moves no data and costs no space
    # until the zvol diverges from it.
    #
    # Every method here resolves its snapshot from ``snapshot.volume_name``
    # and ``snapshot.name``, the same name-based approach the volume and
    # export paths use, so nothing depends on an id staying valid.
    # ------------------------------------------------------------------

    def _snapshot_id(self, snapshot):
        """Build the appliance id for a Cinder snapshot.

        Args:
            snapshot: The Cinder snapshot

        Returns:
            Full snapshot id, ``pool/volume@snapshot``
        """
        return self.client.snapshot_id(
            self.configuration.truenas_pool,
            snapshot.volume_name,
            snapshot.name)

    def create_snapshot(self, snapshot):
        """Snapshot the zvol backing a Cinder volume.

        Args:
            snapshot: The Cinder snapshot to create

        Raises:
            VolumeBackendAPIException: If the appliance refused
        """
        pool = self.configuration.truenas_pool
        dataset = '%s/%s' % (pool, snapshot.volume_name)
        try:
            self.client.create_snapshot(dataset, snapshot.name)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not snapshot %(dataset)s as %(name)s: '
                       '%(err)s')
                % {'dataset': dataset, 'name': snapshot.name, 'err': exc})

        LOG.info('Created snapshot %(dataset)s@%(name)s.',
                 {'dataset': dataset, 'name': snapshot.name})

    def delete_snapshot(self, snapshot):
        """Delete a Cinder snapshot from the appliance.

        Idempotent: a snapshot that is already gone counts as deleted, so a
        retried delete converges instead of wedging the snapshot in
        ``error_deleting``.

        **Never deferred.** ``delete_snapshot(defer=True)`` would tell ZFS
        to destroy the snapshot once its last clone is released, which
        reports success now and destroys data later, out of Cinder's sight.
        A snapshot a clone depends on is reported busy instead.

        Args:
            snapshot: The Cinder snapshot to delete

        Raises:
            SnapshotIsBusy: If a clone still depends on the snapshot
            VolumeBackendAPIException: For any other failure
        """
        snapshot_id = self._snapshot_id(snapshot)
        try:
            self.client.delete_snapshot(snapshot_id)
        except api_client.TrueNASAPINotFoundError:
            LOG.info('Snapshot %s was already gone; treating the delete as '
                     'complete.', snapshot_id)
        except api_client.TrueNASAPIError as exc:
            self._raise_snapshot_delete_failure(snapshot, snapshot_id, exc)
        else:
            LOG.info('Deleted snapshot %s.', snapshot_id)

    def _raise_snapshot_delete_failure(self, snapshot, snapshot_id, exc):
        """Re-raise a failed snapshot delete, naming any dependent clones.

        The appliance reports the clone case as errno 22 under an
        ``options.defer`` key, which is both undocumented and indirect --
        it is really telling the caller to pass ``defer``, which this
        driver deliberately will not do. Following
        :meth:`_raise_delete_failure`, this asks the appliance what depends
        on the snapshot instead of parsing that message, which costs a
        request only on the failure path and names the blocker.

        Args:
            snapshot: The Cinder snapshot whose delete failed
            snapshot_id: Its appliance id
            exc: The error the delete raised

        Raises:
            SnapshotIsBusy: If a clone still depends on it. The manager
                returns the snapshot to ``available``, which is correct --
                it still exists and is still usable.
            VolumeBackendAPIException: For any other failure
        """
        try:
            clones = (self.client.get_snapshot(snapshot_id)
                      .get('properties', {}).get('clones', {})
                      .get('value') or '')
        except api_client.TrueNASAPIError:
            # Asking was a courtesy. Report the original failure.
            clones = ''

        if clones:
            LOG.error('Cannot delete snapshot %(id)s: %(clones)s still '
                      'depend on it. Delete the volumes cloned from this '
                      'snapshot first; the driver will not defer the '
                      'destroy, because that reports success now and '
                      'destroys data later.',
                      {'id': snapshot_id, 'clones': clones})
            raise exception.SnapshotIsBusy(snapshot_name=snapshot.name)

        raise exception.VolumeBackendAPIException(
            data=_('Could not delete snapshot %(id)s: %(err)s')
            % {'id': snapshot_id, 'err': exc})

    def manage_existing_snapshot(self, snapshot, existing_ref):
        """Bring an existing ZFS snapshot under Cinder management.

        Adopts by rename, as :meth:`manage_existing` does, so the snapshot
        afterwards resolves from ``snapshot.volume_name`` and
        ``snapshot.name`` like any other.

        Args:
            snapshot: The Cinder snapshot to adopt it as
            existing_ref: ``{'source-name': '<pool>/<zvol>@<snapshot>'}``

        Raises:
            ManageExistingInvalidReference: If the reference is malformed,
                names nothing, or names a snapshot of a different volume
            VolumeBackendAPIException: If the rename failed
        """
        source_id, _dataset, source_name = self._parse_existing_snapshot_ref(
            snapshot, existing_ref)

        try:
            self.client.rename_snapshot(source_id, snapshot.name)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not adopt snapshot %(source)s as %(name)s: '
                       '%(err)s')
                % {'source': source_id, 'name': snapshot.name, 'err': exc})

        LOG.info('Adopted snapshot %(source)s as %(name)s on volume '
                 '%(volume)s.',
                 {'source': source_name, 'name': snapshot.name,
                  'volume': snapshot.volume_name})

    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        """Report the size Cinder should record for an adopted snapshot.

        Cinder requires a snapshot's size to match its volume's, and a ZFS
        snapshot has no size of its own -- it references the zvol's blocks
        -- so this reports the **parent zvol's** size. ``used`` would be the
        space the snapshot uniquely pins, which is near zero when it is
        taken and is not what Cinder is asking for.

        Args:
            snapshot: The Cinder snapshot to adopt it as
            existing_ref: ``{'source-name': '<pool>/<zvol>@<snapshot>'}``

        Returns:
            The parent zvol's size in GiB, rounded up

        Raises:
            ManageExistingInvalidReference: If the reference is malformed
                or names nothing adoptable
            VolumeBackendAPIException: If the appliance could not be read
        """
        self._parse_existing_snapshot_ref(snapshot, existing_ref)
        # Validated above as sitting on this volume's zvol, so the parent is
        # snapshot.volume_name by construction.
        return self._zvol_size_gb(
            self._adoptable_zvol(existing_ref, snapshot.volume_name))

    def unmanage_snapshot(self, snapshot):
        """Release a snapshot from Cinder without destroying it.

        **Deliberately makes no API call**, for the same reason as
        :meth:`unmanage`: Cinder calls this *instead of*
        ``delete_snapshot()``, so the only thing left to decide is the ZFS
        snapshot's fate, and for an unmanage that is to leave it alone.

        The snapshot keeps the name Cinder gave it, so it can be adopted
        again with ``source-name: <pool>/<volume>@<snapshot.name>``.

        Args:
            snapshot: The Cinder snapshot to stop managing
        """
        LOG.info('Stopped managing snapshot %(id)s. The ZFS snapshot was '
                 'left in place; delete it by hand if it is not wanted.',
                 {'id': self._snapshot_id(snapshot)})

    def _parse_existing_snapshot_ref(self, snapshot, existing_ref):
        """Validate a snapshot adoption reference.

        Args:
            snapshot: The Cinder snapshot being adopted into
            existing_ref: The reference Cinder passed through

        Returns:
            ``(source_id, dataset, name)`` -- the full snapshot id, its
            dataset path, and the bare snapshot name

        Raises:
            ManageExistingInvalidReference: With a reason naming what was
                wrong
            VolumeBackendAPIException: If the appliance could not be read
        """
        pool = self.configuration.truenas_pool
        expected_dataset = '%s/%s' % (pool, snapshot.volume_name)
        example = _("Expected {'source-name': "
                    "'%(dataset)s/<zvol>@<snapshot>'}.") % {'dataset': pool}

        if not isinstance(existing_ref, dict):
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_('The reference is not a mapping. %s') % example)

        source = existing_ref.get('source-name')
        if not isinstance(source, str) or not source.strip():
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("No 'source-name' was given. %s") % example)
        source = source.strip()

        dataset, sep, name = source.rpartition('@')
        if not sep or not name:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(source)s' does not name a snapshot. %(example)s")
                % {'source': source, 'example': example})

        # The snapshot must sit on the zvol backing the volume Cinder
        # attached this snapshot record to. Adopting one from a different
        # dataset would produce a record whose id is derived from
        # `snapshot.volume_name` and so resolves to something that does not
        # exist -- undeletable through Cinder from the moment it is made.
        if dataset != expected_dataset:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(source)s' is a snapshot of '%(dataset)s', but "
                         "this snapshot belongs to volume '%(volume)s', "
                         "whose zvol is '%(expected)s'. A snapshot can only "
                         "be adopted onto its own volume.")
                % {'source': source, 'dataset': dataset,
                   'volume': snapshot.volume_name,
                   'expected': expected_dataset})

        try:
            self.client.get_snapshot(source)
        except api_client.TrueNASAPINotFoundError:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("No snapshot '%(source)s' exists on the appliance.")
                % {'source': source})
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not read snapshot %(source)s from the '
                       'appliance: %(err)s')
                % {'source': source, 'err': exc})

        return source, dataset, name

    def extend_volume(self, volume, new_size):
        """Grow the zvol backing a Cinder volume.

        ZFS grows a zvol online, so no reconnect is needed and an attached
        volume keeps serving throughout. The guest still has to notice --
        that is Nova's business, not this driver's.

        Growth only: ZFS rejects a ``volsize`` below current usage, and
        Cinder never asks to shrink.

        Args:
            volume: The Cinder volume to grow
            new_size: Desired size in GiB

        Raises:
            VolumeBackendAPIException: If the appliance refused
        """
        pool = self.configuration.truenas_pool
        try:
            self.client.resize_zvol(pool, volume.name, new_size)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not extend volume %(name)s to %(size)s GiB: '
                       '%(err)s')
                % {'name': volume.name, 'size': new_size, 'err': exc})

        LOG.info('Extended %(pool)s/%(name)s to %(size)s GiB.',
                 {'pool': pool, 'name': volume.name, 'size': new_size})

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create a volume from a snapshot, by cloning it.

        A ZFS clone is instant and initially free: it shares the
        snapshot's blocks and grows only as it diverges. That is the point
        of doing it this way rather than copying.

        **The clone is not promoted.** Promotion does not sever the
        dependency, it reverses it -- and it moves the snapshot onto the
        clone, where this driver could no longer resolve it, since snapshot
        ids are derived from ``snapshot.volume_name``. Verified in #13; see
        AGENTS.md.

        The consequence is worth stating plainly, because operators will
        meet it: the source snapshot, and the volume it belongs to, cannot
        be deleted while this volume exists. Both report themselves busy
        rather than failing obscurely. Deleting *this* volume is always
        fine.

        Args:
            volume: The Cinder volume to create
            snapshot: The snapshot to clone from

        Raises:
            VolumeBackendAPIException: If the clone failed
        """
        pool = self.configuration.truenas_pool
        snapshot_id = self._snapshot_id(snapshot)
        try:
            self.client.clone_snapshot(snapshot_id, pool, volume.name)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not create volume %(name)s from snapshot '
                       '%(snapshot)s: %(err)s')
                % {'name': volume.name, 'snapshot': snapshot_id,
                   'err': exc})

        LOG.info('Created %(pool)s/%(name)s as a clone of %(snapshot)s. The '
                 'snapshot and its volume cannot be deleted until this '
                 'volume is.',
                 {'pool': pool, 'name': volume.name,
                  'snapshot': snapshot_id})

    def create_cloned_volume(self, volume, src_vref):
        """Copy an existing volume, by snapshotting it and cloning that.

        ZFS cannot clone a dataset directly, only a snapshot of one, so
        this takes a snapshot first. That snapshot then **has to stay** --
        the clone's blocks are defined by it -- so it is named to be
        recognisable rather than hidden, and it is what makes the source
        volume report itself busy on delete until this volume is gone.

        The snapshot is reclaimed by :meth:`delete_volume` when this
        volume is deleted. It has no Cinder object of its own, so nothing
        else could ever remove it, and leaving it would block the source
        volume's delete permanently rather than temporarily.

        Not promoted, for the reasons in
        :meth:`create_volume_from_snapshot`.

        Args:
            volume: The Cinder volume to create
            src_vref: The volume to copy

        Raises:
            VolumeBackendAPIException: If the snapshot or the clone failed
        """
        pool = self.configuration.truenas_pool
        dataset = '%s/%s' % (pool, src_vref.name)
        snap_name = self._clone_source_snapshot_name(volume)
        snapshot_id = self.client.snapshot_id(pool, src_vref.name, snap_name)

        try:
            self.client.create_snapshot(dataset, snap_name)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not snapshot %(source)s in order to clone it '
                       'as %(name)s: %(err)s')
                % {'source': dataset, 'name': volume.name, 'err': exc})

        try:
            self.client.clone_snapshot(snapshot_id, pool, volume.name)
        except api_client.TrueNASAPIError as exc:
            # The snapshot is useless without its clone, and leaving it
            # would pin the source volume against deletion for a copy that
            # does not exist.
            self.client.best_effort_delete(
                self.client.delete_snapshot, snapshot_id,
                what=f'clone-source snapshot {snapshot_id}')
            raise exception.VolumeBackendAPIException(
                data=_('Could not clone %(source)s as %(name)s: %(err)s')
                % {'source': dataset, 'name': volume.name, 'err': exc})

        LOG.info('Cloned %(source)s as %(pool)s/%(name)s via %(snapshot)s. '
                 'That snapshot is retained because the clone depends on '
                 'it, and %(source)s cannot be deleted until this volume '
                 'is.',
                 {'source': dataset, 'pool': pool, 'name': volume.name,
                  'snapshot': snapshot_id})

    def _reclaimable_clone_source(self, pool, volume):
        """Return the clone-source snapshot backing this volume, if any.

        Only ever returns a snapshot **this driver created to serve a
        clone**. A volume made by :meth:`create_volume_from_snapshot` also
        has an ``origin``, but that origin is a real Cinder snapshot with
        its own lifecycle and its own delete path -- removing it here would
        destroy an object Cinder still believes exists.

        The two are told apart by name, which is why
        :meth:`_clone_source_snapshot_name` builds a distinctive one.

        Never raises. The lookup is a courtesy ahead of a delete that has
        to proceed regardless.

        Args:
            pool: Pool the zvol lives in
            volume: The Cinder volume about to be deleted

        Returns:
            The full snapshot id to reclaim, or None
        """
        try:
            zvol = self.client.get_zvol(pool, volume.name)
        except api_client.TrueNASAPIError:
            return None

        origin = zvol.get('origin')
        if isinstance(origin, dict):
            origin = origin.get('rawvalue')
        if not origin:
            return None

        _dataset, _sep, name = str(origin).rpartition('@')
        return origin if self._is_clone_source_snapshot(name) else None

    def _is_clone_source_snapshot(self, snapshot_name):
        """Decide whether a snapshot name is one taken to back a clone.

        Args:
            snapshot_name: Bare snapshot name, without the dataset or ``@``

        Returns:
            True if this driver created it for :meth:`create_cloned_volume`
        """
        return snapshot_name.startswith(self._clone_source_prefix())

    def _clone_source_prefix(self):
        """Return the naming prefix shared by clone-source snapshots.

        Returns:
            The prefix, carrying ``snapshot_name_template``'s own
        """
        template = (self.configuration.safe_get('snapshot_name_template')
                    or DEFAULT_SNAPSHOT_NAME_TEMPLATE)
        prefix = template.split('%s')[0] or 'snapshot-'
        return '%sclone-src-' % prefix

    def _clone_source_snapshot_name(self, volume):
        """Name the snapshot a clone is taken from.

        Carries ``snapshot_name_template``'s prefix deliberately. The
        snapshot outlives the call and will show up as a blocker when
        somebody tries to delete the source volume, and
        :meth:`_is_cinder_snapshot` decides whether that message says
        "Cinder made this" or "something else on the appliance is
        snapshotting your volumes". Without the prefix it would be the
        second, which is both wrong and sends the reader hunting a
        periodic-snapshot task that does not exist.

        Args:
            volume: The Cinder volume being created by the clone

        Returns:
            A snapshot name, unique to that volume
        """
        return '%s%s' % (self._clone_source_prefix(), volume.name)

    # ------------------------------------------------------------------
    # Adoption (#20)
    #
    # The migration this driver exists for: every disk already lives on the
    # appliance as a zvol, and copying an estate of them out and back is
    # not viable. Adoption renames a zvol into Cinder's naming convention
    # in place, which ZFS does as a metadata operation -- no data moves,
    # whatever the disk's size.
    #
    # Cinder calls manage_existing_get_size() first and manage_existing()
    # second, so both validate the reference; the first call's answer is
    # not carried over.
    # ------------------------------------------------------------------

    def manage_existing_get_size(self, volume, existing_ref):
        """Report the size Cinder should record for a zvol being adopted.

        Args:
            volume: The Cinder volume the zvol will be adopted as
            existing_ref: ``{'source-name': '<pool>/<zvol>'}``

        Returns:
            The zvol's size in GiB, rounded up

        Raises:
            ManageExistingInvalidReference: If the reference is malformed
                or names nothing adoptable
            VolumeBackendAPIException: If the appliance could not be read
        """
        name = self._parse_existing_ref(existing_ref)
        return self._zvol_size_gb(self._adoptable_zvol(existing_ref, name))

    def manage_existing(self, volume, existing_ref):
        """Bring an existing zvol under Cinder management.

        Adopts by rename, the first of the two strategies ``BaseVD``
        describes. It suits this driver because every other operation
        already resolves the backend object by name -- ``delete_volume``,
        ``create_export`` and ``remove_export`` all derive it from
        ``volume.name`` -- so once the zvol carries that name it is
        indistinguishable from one the driver created.

        Returns nothing: ``provider_location`` is built by
        :meth:`create_export` when the volume is first attached, not here.

        Args:
            volume: The Cinder volume to adopt the zvol as
            existing_ref: ``{'source-name': '<pool>/<zvol>'}``

        Raises:
            ManageExistingInvalidReference: If the reference is malformed,
                names nothing adoptable, or names a zvol that is still
                exported
            VolumeBackendAPIException: If the rename failed
        """
        pool = self.configuration.truenas_pool
        name = self._parse_existing_ref(existing_ref)
        self._adoptable_zvol(existing_ref, name)
        self._clear_conflicting_export(existing_ref, pool, name)

        try:
            self.client.rename_zvol(pool, name, volume.name)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not adopt %(pool)s/%(name)s as volume '
                       '%(volume)s: %(err)s')
                % {'pool': pool, 'name': name, 'volume': volume.name,
                   'err': exc})

        LOG.info('Adopted zvol %(pool)s/%(name)s as volume %(volume)s. '
                 'The rename moved no data.',
                 {'pool': pool, 'name': name, 'volume': volume.name})

    def unmanage(self, volume):
        """Release a volume from Cinder without destroying its zvol.

        **Deliberately makes no API call.** Cinder's delete path invokes
        ``remove_export()`` and then calls this *instead of*
        ``delete_volume()`` (``cinder/volume/manager.py``), so the export
        is already gone by the time this runs and the only thing left to
        decide is the zvol's fate -- which for an unmanage is to leave it
        exactly where it is.

        The zvol keeps the Cinder name it was given, so it can be adopted
        again later with ``source-name: <pool>/<volume.name>``.

        Args:
            volume: The Cinder volume to stop managing
        """
        LOG.info('Stopped managing volume %(volume)s. The zvol '
                 '%(pool)s/%(volume)s was left in place with its data '
                 'intact; delete it by hand if it is not wanted.',
                 {'pool': self.configuration.truenas_pool,
                  'volume': volume.name})

    def _parse_existing_ref(self, existing_ref):
        """Validate an adoption reference and return the zvol name.

        Args:
            existing_ref: The reference Cinder passed through from the API

        Returns:
            The zvol name relative to the configured pool

        Raises:
            ManageExistingInvalidReference: With a reason naming what was
                wrong and what a correct reference looks like
        """
        pool = self.configuration.truenas_pool
        example = _("Expected {'source-name': '%(pool)s/<zvol>'}, for "
                    "example '%(pool)s/vm-100-disk-0'.") % {'pool': pool}

        if not isinstance(existing_ref, dict):
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_('The reference is not a mapping. %s') % example)

        source = existing_ref.get('source-name')
        if not isinstance(source, str) or not source.strip():
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("No 'source-name' was given. %s") % example)
        source = source.strip()

        # Checked before the pool prefix, so naming a snapshot gets the
        # answer that helps rather than "wrong pool".
        if '@' in source:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(source)s' names a snapshot. Adopt a snapshot "
                         "with 'cinder snapshot-manage', not this call.")
                % {'source': source})

        prefix = '%s/' % pool
        if not source.startswith(prefix):
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(source)s' is not in pool '%(pool)s', which is "
                         "the only pool this backend manages. %(example)s")
                % {'source': source, 'pool': pool, 'example': example})

        name = source[len(prefix):]
        if not name:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(source)s' names the pool itself rather than a "
                         "zvol in it. %(example)s")
                % {'source': source, 'example': example})
        return name

    def _adoptable_zvol(self, existing_ref, name):
        """Fetch the zvol named by a reference, refusing anything else.

        The type check is not redundant. A ``GET`` on a filesystem dataset
        answers 200, so a reference naming a filesystem reaches this point
        looking exactly like a successful lookup.

        Args:
            existing_ref: The reference, for the exception message
            name: Zvol name relative to the pool

        Returns:
            The zvol's metadata

        Raises:
            ManageExistingInvalidReference: If nothing is there, or what is
                there is not a zvol
            VolumeBackendAPIException: If the appliance could not be read
        """
        pool = self.configuration.truenas_pool
        try:
            zvol = self.client.get_zvol(pool, name)
        except api_client.TrueNASAPINotFoundError:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("No dataset '%(pool)s/%(name)s' exists on the "
                         "appliance.") % {'pool': pool, 'name': name})
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not read %(pool)s/%(name)s from the '
                       'appliance: %(err)s')
                % {'pool': pool, 'name': name, 'err': exc})

        kind = zvol.get('type')
        if kind != 'VOLUME':
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(pool)s/%(name)s' is a %(kind)s, not a zvol. "
                         "Only zvols can back a Cinder volume.")
                % {'pool': pool, 'name': name, 'kind': kind or _('unknown')})
        return zvol

    def _zvol_size_gb(self, zvol):
        """Return a zvol's size in GiB, rounded up.

        **Rounded up, never down.** Cinder treats the returned size as the
        volume's capacity, and reporting less than the zvol actually holds
        would let it believe data fits where it does not.

        The arithmetic is integer throughout: ``volsize`` is a byte count
        and float division would start losing precision on a large enough
        zvol.

        Args:
            zvol: Zvol metadata from the appliance

        Returns:
            Size in GiB

        Raises:
            VolumeBackendAPIException: If the appliance reported no usable
                volsize
        """
        raw = zvol.get('volsize')
        # volsize arrives as {'parsed': int, 'rawvalue': str, 'value': str,
        # ...}; `parsed` is already an integer, `rawvalue` the same number
        # as a string. A bare value is accepted too so the shape is not
        # load-bearing.
        if isinstance(raw, dict):
            raw = raw.get('parsed', raw.get('rawvalue'))
        try:
            size_bytes = int(raw)
        except (TypeError, ValueError):
            raise exception.VolumeBackendAPIException(
                data=_('The appliance reported no usable volsize for '
                       '%(name)s: %(raw)r')
                % {'name': zvol.get('name'), 'raw': zvol.get('volsize')})
        if size_bytes <= 0:
            raise exception.VolumeBackendAPIException(
                data=_('The appliance reported %(name)s as %(size)s bytes, '
                       'which cannot be adopted.')
                % {'name': zvol.get('name'), 'size': size_bytes})
        return (size_bytes + GIB - 1) // GIB

    def _clear_conflicting_export(self, existing_ref, pool, name):
        """Ensure nothing is exporting the zvol before it is renamed.

        **This gate exists because the appliance has none.** Both rename
        endpoints require ``force``, and refuse without it even when the
        dataset is idle, so TrueNAS performs no safety check on the
        driver's behalf -- it renames whatever it is pointed at, including
        a zvol a live initiator is writing to.

        Two cases, deliberately treated differently:

        A **live session** is refused unconditionally. Renaming a zvol out
        from under a connected initiator breaks it mid-write, and no
        configuration option should be able to authorise that.

        A **configured but idle export** is refused by default, naming what
        to remove, and removed automatically when
        ``truenas_adopt_removes_export`` is set. Hand-provisioned disks
        carry one of these each, so the option is what makes a bulk
        migration bearable; the default keeps the driver from destroying
        iSCSI configuration nobody asked it to touch.

        Args:
            existing_ref: The reference, for the exception message
            pool: Pool the zvol lives in
            name: Zvol name relative to the pool

        Raises:
            ManageExistingInvalidReference: If the zvol is exported and
                this call may not clear it
            VolumeBackendAPIException: If the appliance could not be read,
                or the export could not be removed
        """
        disk = self.client.zvol_disk_path(pool, name)
        try:
            extents = [extent for extent in self.client.get_extents()
                       if extent.get('disk') == disk]
            if not extents:
                LOG.debug('No iSCSI extent references %s; safe to adopt.',
                          disk)
                return
            extent_ids = {extent['id'] for extent in extents}
            links = [link for link in self.client.get_target_extents()
                     if link.get('extent') in extent_ids]
            target_ids = {link['target'] for link in links}
            targets = [target for target in self.client.get_targets()
                       if target['id'] in target_ids]
            sessions = self._sessions_for(targets)
        except api_client.TrueNASAPIError as exc:
            raise exception.VolumeBackendAPIException(
                data=_('Could not determine whether %(disk)s is already '
                       'exported, so it will not be adopted: %(err)s')
                % {'disk': disk, 'err': exc})

        if sessions:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(pool)s/%(name)s' has %(count)d live iSCSI "
                         "session(s), from %(initiators)s. Renaming it now "
                         "would break whatever is using it. Detach it "
                         "first; this is refused whatever "
                         "truenas_adopt_removes_export is set to.")
                % {'pool': pool, 'name': name, 'count': len(sessions),
                   'initiators': ', '.join(sorted(
                       {'%s (%s)' % (session.get('initiator'),
                                     session.get('initiator_addr'))
                        for session in sessions}))})

        described = ', '.join(
            ['target %s' % target['id'] for target in targets]
            + ['extent %s' % extent['id'] for extent in extents])

        if not self.configuration.truenas_adopt_removes_export:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_("'%(pool)s/%(name)s' is already exported over "
                         "iSCSI by %(described)s. Nothing is connected to "
                         "it, so removing that export is safe and leaves "
                         "the zvol untouched -- do it on the appliance and "
                         "retry, or set truenas_adopt_removes_export = "
                         "true to have the driver do it.")
                % {'pool': pool, 'name': name, 'described': described})

        self._remove_conflicting_export(disk, targets, extents)

    def _sessions_for(self, targets):
        """Return the live iSCSI sessions served by any of these targets.

        Sessions name their target by full IQN rather than by id, so the
        comparison is built from the basename resolved during setup.

        Args:
            targets: Target rows from the appliance

        Returns:
            The matching session rows, empty if none
        """
        if not targets:
            return []
        iqns = {'%s:%s' % (self.iscsi_basename, target['name'])
                for target in targets}
        return [session for session in self.client.get_iscsi_sessions()
                if session.get('target') in iqns]

    def _remove_conflicting_export(self, disk, targets, extents):
        """Delete an idle export standing in the way of an adoption.

        Targets before extents, matching :meth:`remove_export`: deleting
        either end cascades the association, and the extent is what pins
        the zvol.

        Args:
            disk: Zvol disk path, for logging
            targets: Target rows to delete
            extents: Extent rows to delete

        Raises:
            VolumeBackendAPIException: If anything could not be removed,
                before the zvol has been renamed
        """
        removed = []
        for what, rows, delete in (
            ('target', targets, self.client.delete_target),
            ('extent', extents, self.client.delete_extent),
        ):
            for row in rows:
                try:
                    delete(row['id'])
                except api_client.TrueNASAPINotFoundError:
                    continue
                except api_client.TrueNASAPIError as exc:
                    raise exception.VolumeBackendAPIException(
                        data=_('Could not remove iSCSI %(what)s %(id)s '
                               'while adopting %(disk)s, so the zvol has '
                               'not been renamed: %(err)s')
                        % {'what': what, 'id': row['id'], 'disk': disk,
                           'err': exc})
                removed.append('%s %s' % (what, row['id']))

        try:
            self.client.reload_iscsi_service()
        except api_client.TrueNASAPIError as exc:
            LOG.warning('Removed %(removed)s for %(disk)s but could not '
                        'reload the iSCSI service: %(err)s',
                        {'removed': ', '.join(removed), 'disk': disk,
                         'err': exc})
        LOG.info('Removed %(removed)s that were exporting %(disk)s, as '
                 'truenas_adopt_removes_export is set. The zvol itself was '
                 'not touched.',
                 {'removed': ', '.join(removed), 'disk': disk})

    def _update_volume_stats(self):
        """Report real capacity so the scheduler will place volumes here.

        **Not optional.** The inherited version reports
        ``free_capacity_gb=0`` with ``reserved_percentage=100``, which the
        scheduler's capacity filter rejects -- the backend would take no
        volumes and nothing would say why.

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

        # Bytes. `free` accounts for reservations; `allocated` does not.
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
