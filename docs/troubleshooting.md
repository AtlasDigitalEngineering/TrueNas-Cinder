# Troubleshooting

Faults this driver has actually produced, what each one looks like from the
outside, and what to do. Every entry here is something that happened during
development or acceptance testing — this is not a list of things that might go
wrong in principle.

**Where the detail is.** Cinder tells an operator very little through the API.
A failed operation leaves a volume in `error` and usually creates no user
message, so `cinder message-list` will not explain it. The driver's reasons go
to `cinder-volume`'s log:

```bash
# Kolla
grep -i truenas /var/log/kolla/cinder/cinder-volume.log
# or, if it logs to the container
docker logs cinder_volume 2>&1 | grep -i truenas
```

With more than one backend, every message this driver writes carries the
backend it came from, so narrow to one appliance first:

```bash
grep '\[truenas-iscsi\]' /var/log/kolla/cinder/cinder-volume.log
```

The name in brackets is `volume_backend_name` from your `cinder.conf`.

---

## The service will not start

### `Missing required TrueNAS driver options in cinder.conf`

`truenas_api_url`, `truenas_api_key` and `truenas_pool` are all required. Under
Kolla the backend section goes in
`/etc/kolla/config/cinder/cinder-volume.conf` on the deploy host — the image
alone does nothing. This is the single most commonly missed deployment step.

### `truenas_api_url is not usable: base_url must not contain inline credentials`

You have `https://user:pass@appliance` in the URL. That does not work and never
did: `requests` turns the userinfo into a Basic header that *overwrites* the
Bearer API key, so every call would 401 — and it leaks the password into logged
tracebacks. Remove the credentials; authentication is `truenas_api_key`.

### `Cannot reach the TrueNAS appliance at ...`

Network, DNS or TLS. If the appliance uses a self-signed certificate, fix the
certificate rather than setting `truenas_verify_ssl = false` — that option
disables verification for every call the driver makes, including the ones
carrying the API key.

### Authentication fails — and the two causes need opposite fixes

The driver distinguishes them, because guessing wrong wastes real time:

| Message | Meaning | Fix |
|---|---|---|
| `TrueNAS rejected truenas_api_key: it is wrong, revoked or expired` | HTTP 401 | Issue a new key. **Not** a role problem. |
| `The TrueNAS account behind truenas_api_key does not have the role this driver needs` | HTTP 403 | Grant `FULL_ADMIN`. **Do not** reissue the key — it is valid. |

The driver needs `FULL_ADMIN`. A least-privilege role set was attempted and
does not work on TrueNAS 25.10; see [configuration.md](configuration.md).

### `truenas_pool = '...' does not exist on the appliance`

The message lists the pools that do exist. Note this is the *pool* name, not a
dataset path.

### `The iSCSI service on the TrueNAS appliance is STOPPED`

The driver refuses to start, deliberately, and does not start the service
itself. **This check is the most valuable one it makes.** With `iscsitarget`
stopped, every export the driver builds still succeeds — targets and extents
are created, Cinder records them, nothing errors — and no initiator can attach
to any of them. Failing loudly at startup is much cheaper than discovering that
per-volume later.

Start it, and set it to start at boot. If it is running but not enabled, the
driver logs a warning rather than failing: it works now and dies at the next
appliance reboot.

### `No iSCSI portal is configured` / `The appliance has N iSCSI portals`

The driver does not create a portal. With exactly one it discovers it and logs
which. With several it refuses to guess — set `truenas_iscsi_portal_id`.

### `snapshot_name_template = '%s' has no text before its %s placeholder`

The prefix is what tells a Cinder snapshot apart from one a periodic snapshot
or replication task took, and the delete path needs that distinction — see
[configuration.md](configuration.md). Set a template with a literal prefix,
such as the default `snapshot-%s`.

### `iSCSI portal N is bound to 0.0.0.0, which is not an address a compute node can connect to`

Either rebind the portal to a static address, or set
`truenas_iscsi_portal_addresses` to the addresses initiators should actually
use. A wildcard bind reports an address nothing can connect to.

---

## The image will not pull

```
Error response from daemon: ... denied
```

**A package first published with `GITHUB_TOKEN` is private even when the
repository is public.** Nothing in the publish workflow can change that —
`GITHUB_TOKEN` does not carry the scope and the REST API does not expose
container visibility — so it is a one-time manual step in the package settings.
The release workflow warns when it detects this. See
[deployment.md](deployment.md).

---

## Attaches are slow, or a volume has no failover

### An attach takes about two and a half minutes

An address in `truenas_iscsi_portal_addresses` is unreachable from that compute
node. Measured: 14.5s with both addresses reachable, ~144s with one blocked —
whichever address, and whether it is rejected or black-holed.

The cost is open-iscsi's login retry budget
(`login_timeout` × `initial_login_retry_max` = 120s by default), not this
driver's or Cinder's.

### `No dm was created, connection to volume is probably bad and will perform poorly`

The same fault, and this is the part that matters. The attach **succeeds** —
the volume reports `in-use` and every API surface looks healthy — but no
device-mapper node was created, so the instance has a single path and no
failover. A deployment configured for redundancy does not have it, and only
this log line says so.

It is also sticky: fixing the network does not repair an existing attachment.
Detach and re-attach.

Check reachability from **every compute node**, not from the controller:

```bash
for ip in 10.20.21.81 10.40.96.182; do
  timeout 5 bash -c "cat < /dev/null > /dev/tcp/$ip/3260" \
    && echo "$ip:3260 reachable" || echo "$ip:3260 UNREACHABLE"
done
```

and confirm a multipathed attach really is one:

```bash
sudo multipath -ll          # expect one path per group
sudo iscsiadm -m session    # expect one session per address, per volume
```

See [configuration.md](configuration.md) for the full measurements.

### `An iSCSI extent named ... already exists on the appliance but is backed by ...`

An extent of the right name exists but points at a different zvol. The driver
refuses rather than adopting it, because adopting it would export another
volume's data into this instance. Remove or rename it on the appliance.

---

## A volume or snapshot will not delete

### `Cannot delete ...: N Cinder snapshot(s) still exist`

Cinder deletes snapshots before volumes, so reaching this state means something
deleted them out of order. Delete the snapshots through Cinder first.

### `Cannot delete ...: N snapshot(s) still depend on it, and M of them were not created by Cinder`

Something else on the appliance is snapshotting a Cinder-managed volume — check
for a periodic snapshot or replication task covering this pool. **The driver
will not delete snapshots it does not own.** Either remove them on the
appliance, or exclude the pool from whatever is creating them.

### `Cannot delete snapshot ...: <clones> still depend on it`

ZFS will not destroy a snapshot with clones. Delete the volumes cloned from it
first.

The driver deliberately does not defer the destroy, and does not promote the
clone to break the dependency. Deferring reports success now and destroys data
later; promoting *moves* the snapshot to the clone rather than severing
anything, which changes what a later delete would remove.

---

## Adoption (`cinder manage`) fails

### The volume goes to `error` and tells you nothing

`size` stays `0` and there is no user message. **`size = 0` is not diagnostic** —
the manage flow writes the size only after `manage_existing` succeeds, so a
refusal at any point leaves it zero. It does not mean sizing failed.

The reason is in `cinder-volume`'s log:

```bash
grep -i "invalid backend reference" /var/log/kolla/cinder/cinder-volume.log
```

Delete the errored volume record before retrying. It is safe: the record's name
never existed on the appliance, so `delete_volume` finds nothing and treats the
delete as complete. The zvol is untouched.

### `... is already exported over iSCSI by target N, extent M`

A hand-provisioned disk usually has an export made by hand. Either delete the
named objects, or set `truenas_adopt_removes_export = true` and let the driver
remove them. The zvol and its data are untouched either way.

The refusal names **only** the objects blocking this adoption.

### `in use: N live iSCSI session(s) from ...`

Refused, and this one is **not** configurable at any setting. Renaming a disk
out from under a running machine corrupts it mid-write. Stop whatever is using
it and confirm on the appliance:

```bash
curl -sk -X POST -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<appliance>/api/v2.0/iscsi/global/sessions" -d '{}' | jq -r '.[].target'
```

### `A snapshot can only be adopted onto its own volume`

`cinder snapshot-manage` requires the snapshot to belong to the zvol backing
the Cinder volume you name. The driver resolves snapshots from their volume's
name, so a record adopted onto a different volume could never be resolved — or
deleted — again. Adopt the volume first, then its snapshots.

### `cinder: Application credentials cannot request a scope`

Not a driver fault. The `cinder` shell always requests a scoped token and
application credentials forbid it. Use username/password for the migration, or
drive the API directly — see [migration.md](migration.md).

---

## Housekeeping

### Objects left behind on the appliance

Anything that fails between creating an export and persisting the model update
can leave a target or extent behind. `remove_export` cleans up on the failure
path, but it cannot run if `cinder-volume` died. Across a migration of hundreds
of disks those accumulate until an appliance limit is hit — and the symptom is
a *create* failing for an apparently unrelated reason.

```bash
python3 tools/find_orphans.py --backend <host@backend>          # report
python3 tools/find_orphans.py --backend <host@backend> --delete-exports
```

**`--delete-exports` never removes a zvol.** Targets, extents and links are
wrappers that can be rebuilt; a zvol is the disk. A zvol with no Cinder volume
may be a leak or a volume whose Cinder record was lost, and those are
indistinguishable from the appliance side — so they are reported for a human to
decide and the tool will not act on them at any flag.

It also distinguishes **adoption candidates** from leaks. A zvol named
`vm-100-disk-0` was made by somebody else and is a perfectly healthy
`manage_existing` candidate; only `volume-<uuid>` names are the driver's.

### Duplicate initiator groups

TrueNAS enforces no uniqueness on initiator groups, and concurrent attaches
from one compute node used to race and create one per caller — six concurrent
calls produced six groups. That is fixed (the driver serialises the lookup),
but groups created before the fix persist. `find_orphans.py` reports them.
