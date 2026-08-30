# Configuring the TrueNAS Cinder driver

## Appliance prerequisites

The driver **validates** these at startup and refuses to run without them. It
never creates or changes them: they are appliance-wide, shared by every volume
and every `cinder-volume` worker, so provisioning a volume must not have them as
a side effect.

1. **An API key for a service account.** Credentials → API Keys in the TrueNAS
   UI. The driver authenticates with a Bearer key, not a username and password.
2. **A ZFS pool** for volumes. Volumes are created in it as zvols.
3. **The iSCSI service running, and enabled at boot.** System Settings →
   Services → iSCSI. If it is stopped the driver refuses to start — with good
   reason: the appliance accepts target and extent configuration while the
   service is down, reports no error, and nothing attaches. If it is running but
   not enabled, the driver warns: it works now and breaks at the next reboot.
4. **An iSCSI portal bound to a statically configured address.** Shares → Block
   Shares (iSCSI) → Portals. TrueNAS offers only static addresses as portal
   addresses — a DHCP interface is not selectable at all. A portal bound to
   `0.0.0.0` is accepted by the appliance but is not an address a compute node
   can connect to, so the driver requires `truenas_iscsi_portal_addresses` in
   that case.

## Sample backend section

```ini
[DEFAULT]
enabled_backends = truenas-iscsi

[truenas-iscsi]
volume_driver = truenas_cinder_driver.driver.TrueNASISCSIDriver
volume_backend_name = truenas-iscsi

# Required.
truenas_api_url = https://truenas.example.com
truenas_api_key = <service account API key>
truenas_pool = tank

# Only needed when the appliance has more than one portal. With exactly one,
# the driver discovers it and logs which it chose.
#truenas_iscsi_portal_id = 1

# Addresses initiators should use, in preference order. Required when the
# portal binds 0.0.0.0 or ::. Listing more than one advertises multipath:
# the driver binds the target to every portal and Cinder presents them as
# target_portals / target_iqns / target_luns.
#truenas_iscsi_portal_addresses = 10.20.21.81,10.40.96.182

# Leave enabled. Fix the appliance certificate rather than turning this off.
truenas_verify_ssl = true

# Only consulted by `cinder manage`. Off means the driver refuses to adopt a
# zvol that already has an iSCSI export and tells you what to remove; on means
# it removes that export itself. Either way it refuses a zvol with a live
# session. See "Adopting existing zvols" below.
#truenas_adopt_removes_export = false
```

`truenas_api_key` is declared `secret=True`, so oslo_config redacts it from
logged configuration dumps. It is still plain text in `cinder.conf` — protect
that file as you would any other credential store.

## Options

| Option | Type | Default | Required |
|---|---|---|---|
| `truenas_api_url` | string | — | yes |
| `truenas_api_key` | string (secret) | — | yes |
| `truenas_pool` | string | — | yes |
| `truenas_iscsi_portal_id` | integer | discovered | only with several portals |
| `truenas_iscsi_portal_addresses` | list | portal's own addresses | when the portal binds a wildcard |
| `truenas_verify_ssl` | boolean | `true` | no |
| `truenas_adopt_removes_export` | boolean | `false` | no |

## Let OpenStack own snapshots

TrueNAS does not snapshot zvols on its own, so this is not something you have
to turn off — but it *is* something to avoid turning on for a pool Cinder
manages.

ZFS refuses to destroy a zvol that still has snapshots. The driver will not
destroy snapshots it did not create: it fails the delete and returns the volume
to `available` rather than silently discarding them. So anything that creates
snapshots behind Cinder's back — a **periodic snapshot task**, a **replication
task**, or a manual snapshot — will make that volume undeletable until the
snapshot is removed, and Cinder cannot tell you why beyond reporting the volume
as busy.

**Snapshot Cinder volumes through Cinder.** If you need TrueNAS-side snapshot or
replication tasks, scope them to datasets Cinder does not manage, or give Cinder
its own pool.

There is no good automatic answer here. Deleting foreign snapshots to complete a
delete would be the driver destroying data it does not own, which is worse than
failing.

## Adopting existing zvols

`cinder manage` brings a zvol the driver did not create under Cinder's
management. The zvol is **renamed**, not copied, so adoption costs the same
whether the disk is 1 GiB or 10 TiB.

```bash
openstack volume manage \
  --name <name> --volume-type truenas-iscsi \
  <cinder-host>@truenas-iscsi#truenas-iscsi \
  <pool>/<zvol>
```

The reference is the zvol's full path in the pool this backend manages, and it
may be nested — `Dev-Pool/proxmox/vm-100-disk-0` is fine. Adoption moves it to
the pool root under Cinder's naming convention.

### The export conflict

A hand-provisioned disk usually already has an iSCSI extent and target. **The
appliance performs no safety check here**: both rename endpoints require
`force` and refuse without it even when the dataset is idle, so TrueNAS will
rename whatever it is pointed at, including a zvol an initiator is writing to.
The driver therefore checks first, and behaves in one of three ways.

| Zvol state | Behaviour |
|---|---|
| No export | Adopted. |
| Export exists, nothing connected | Refused by default, naming the target and extent to delete. With `truenas_adopt_removes_export = true`, the driver deletes them and adopts. |
| Live iSCSI session | **Always refused**, whatever the option is set to. |

The last row is not configurable on purpose: renaming a zvol out from under a
connected initiator breaks it mid-write, and no configuration should be able to
authorise that. Detach the disk first.

Removing an export never touches the zvol or its data — it deletes the iSCSI
objects that point at it, and Cinder builds its own on the first attach.

### Releasing a volume again

`cinder unmanage` removes Cinder's record and **leaves the zvol in place** with
its data intact. The zvol keeps the name Cinder gave it, so it can be adopted
again later with `source-name: <pool>/<volume-name>`. Delete it by hand if you
do not want it — nothing else will.

### Adopting snapshots

A snapshot is adopted onto the volume it belongs to, and only that volume:

```bash
cinder snapshot-manage --id-type source-name --name <name> \
  <volume> <pool>/<zvol>@<snapshot>
```

Adopt the volume first. A snapshot named on any other volume is refused — the
driver resolves snapshots through their volume's name, so such a record could
never be resolved, and so could never be deleted through Cinder.

`cinder snapshot-unmanage` releases it and leaves the ZFS snapshot in place.

The step-by-step procedure for a whole estate is in
[migration.md](migration.md).

## Clones share blocks, and that has a visible cost

`create-from-snapshot` and `volume clone` both produce a **ZFS clone**: a
writable dataset sharing the source's blocks. It is created instantly and
consumes nothing until it diverges, whatever the size of the source. That is
the reason to do it this way.

The cost is a dependency that ZFS will not let you ignore. Until the derived
volume is deleted:

- the **snapshot** it was cloned from cannot be deleted — `cinder
  snapshot-delete` reports the snapshot busy and leaves it `available`;
- the **volume** that snapshot belongs to cannot be deleted either — it reports
  busy and stays `available`.

Deleting the derived volume itself always works, and lifts both blocks.

Cloning a volume takes a snapshot of it first, because ZFS can only clone a
snapshot. That snapshot is named `snapshot-clone-src-<volume>` and is kept
while the clone lives — the clone's blocks are defined by it. It is what makes
the source report busy, and it is named as Cinder's so the delete failure says
so rather than blaming an unknown snapshot task.

**It is reclaimed when the clone is deleted.** That matters more than it
sounds: this snapshot has no Cinder object of its own, never appears in
`cinder snapshot-list`, and so could not be removed through Cinder by anyone.
Left behind it would go on blocking the source volume's delete permanently
rather than temporarily, and only someone in the TrueNAS UI who knew to look
for `snapshot-clone-src-*` could clear it. The driver reads the zvol's
`origin` before deleting it and removes that snapshot afterwards, but only
when the name marks it as one taken for a clone — a volume created from a
*Cinder* snapshot also has an origin, and that one belongs to Cinder.

**The driver does not promote clones.** Promotion is often suggested as the fix
for the above; it is not. It reverses the dependency rather than removing it —
the source becomes deletable and the *clone* stops being deletable — and it
moves the snapshot onto the clone, where this driver could no longer resolve
it. Trading a rare annoyance for a constant one is a bad deal, so the
dependency is left pointing the way that keeps the common operations working.

## Volume naming

Cinder's `volume_name_template` becomes the iSCSI target name. TrueNAS accepts
only **lowercase alphanumerics plus `.`, `-` and `:`**, so the default
`volume-%s` is fine but a template with an underscore or a capital is not. The
driver validates the rendered name against the appliance at startup rather than
letting it fail at the first attach.

## Options this driver does not use

`san_ip`, `san_login`, `san_password` and `san_private_key` come from the
`SanISCSIDriver` base class and drive arrays over SSH. This driver only ever
speaks to the REST API, so they are unused and need not be set — its
`check_for_setup_error` deliberately does not call the base class
implementation, which would otherwise demand them.

Older TrueNAS driver notes referenced `iscsi_ip_address`, `iscsi_protocol` and
`target_prefix` in this section. The first two no longer exist in OpenStack
2025.1 — the equivalents are `target_ip_address` and `target_protocol` — and
none of the three is needed: the driver reads the portal address from the
appliance (or from `truenas_iscsi_portal_addresses`) and the IQN prefix from
`GET /iscsi/global`.
