"""
OpenStack Cinder volume driver for TrueNAS Scale over iSCSI.

Configuration, setup validation and capacity reporting. The volume lifecycle
and the export/connection path are issue #3, inherited as
``NotImplementedError`` until then.

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
