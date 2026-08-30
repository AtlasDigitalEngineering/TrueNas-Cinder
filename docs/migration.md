# Migrating existing zvols into Cinder

Adoption brings a zvol the driver did not create under Cinder's management by
**renaming it in place**. No data is copied, so a 10 TiB disk is adopted as
quickly as a 1 GiB one — the rename is a ZFS metadata operation and the zvol's
`creation` timestamp survives it unchanged.

This is the path off a hypervisor whose disks already live on TrueNAS. The disk
stops being managed by the old system and starts being a Cinder volume, without
ever moving.

## Before you start

- The driver must be **deployed and working** — adopt into a backend that has
  already created and attached a volume of its own. Adoption is a poor first
  test, because a failure there looks like a driver problem and a data problem
  at the same time.
- Know your backend's **host string**. It is not the hostname:

  ```bash
  openstack volume show <any-existing-volume> -f value -c os-vol-host-attr:host
  # openstack-test@truenas-iscsi#truenas-iscsi
  ```

- Decide whether `truenas_adopt_removes_export` should be on. See
  [configuration.md](configuration.md); it matters for every disk that was
  provisioned by hand, which is most of them.

## The CLI is `cinder`, not `openstack`

`openstack volume manage` **does not exist**. The OpenStack client has never
implemented the manage/unmanage family, so it comes from `python-cinderclient`:

```bash
pip install python-cinderclient
cinder --version    # 9.9.0 at time of writing
```

Everything below uses `cinder`. It authenticates from the same `OS_*`
environment as `openstack`.

## Finding candidates

`cinder manageable-list` is the intended way and **this driver does not
implement it** — it returns a 500. Enumerate on the appliance instead:

```bash
curl -sk -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<appliance>/api/v2.0/pool/dataset?type=VOLUME&name__^=<pool>/" \
  | jq -r '.[] | "\(.name)\t\(.volsize.value)"'
```

Anything not already named `volume-<uuid>` is a candidate.

## Adopting a volume

One disk at a time:

```bash
cinder manage \
  --id-type source-name \
  --name <name-for-cinder> \
  --volume-type truenas-iscsi \
  --bootable \
  <host> <pool>/<zvol>
```

- `--id-type source-name` is the default, but state it — the alternative
  (`source-id`) is meaningless to this driver and fails confusingly.
- `--bootable` matters when the zvol holds a VM's root disk. Without it Nova
  will not boot from the resulting volume, and the fix afterwards is
  `cinder set-bootable`.
- The zvol may be nested. `<pool>/vms/vm-100-disk-0` is a valid identifier;
  adoption moves it to the pool root under Cinder's naming convention.

### Shut the disk down first

Adoption is refused while an initiator holds an iSCSI session on the zvol, and
that refusal is not configurable. Renaming a disk out from under a running
machine corrupts it mid-write.

Stop the machine using the disk, and confirm the appliance agrees before
adopting:

```bash
curl -sk -X POST -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<appliance>/api/v2.0/iscsi/global/sessions" -d '{}' | jq -r '.[].target'
```

### If the zvol already has an iSCSI export

A hand-provisioned disk usually has an extent and a target made by hand. What
happens depends on `truenas_adopt_removes_export`:

| Setting | Behaviour |
|---|---|
| `false` (default) | Refused, naming the exact target and extent to delete. Delete them on the appliance and retry. |
| `true` | The driver removes them itself, then adopts. |

Either way the zvol and its data are untouched — only the iSCSI objects
pointing at it are removed, and Cinder builds its own on first attach.

The refusal names **only** the objects blocking this adoption. If it mentions
`target 11` and `extent 8`, those are the two to remove; anything else on the
appliance belongs to something different.

## Verifying an adoption

```bash
cinder show <name> | grep -E 'status|size|bootable|os-vol-host-attr'
```

Then confirm on the appliance that the zvol was **renamed, not copied** — the
old name should be gone and the new one present with the original `creation`:

```bash
curl -sk -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<appliance>/api/v2.0/pool/dataset/id/<pool>%2Fvolume-<uuid>" \
  | jq '{name, creation: .creation.rawvalue, volsize: .volsize.value}'
```

A `creation` matching what the zvol had before adoption proves no data moved.

Finally, boot from it. An adopted volume that attaches but does not boot is
usually missing `--bootable`.

## Size rounding

Cinder records volume sizes in whole GiB, and the driver rounds **up**. A
10.5 GiB zvol is recorded as 11 GiB. The extra gigabyte is a bookkeeping
artefact — the zvol is not resized — but it does count against the project's
quota, so a large migration consumes slightly more quota than the raw capacity
suggests.

Sizes are never rounded down. Telling Cinder a volume is smaller than it is
would let it believe data fits where it does not.

## Adopting snapshots

A zvol's ZFS snapshots come with it. They are simply invisible to Cinder until
adopted individually, and they continue to consume space either way.

```bash
cinder snapshot-manage \
  --id-type source-name \
  --name <name-for-cinder> \
  <volume> <pool>/<zvol>@<snapshot>
```

The `<volume>` is the **already-adopted** Cinder volume, and the snapshot must
be one of that volume's own snapshots. Adopting a snapshot onto a different
volume is refused: the driver resolves snapshots from their volume's name, so
such a record could never be resolved again, and could never be deleted through
Cinder.

Adopt the volume first, then its snapshots.

## Releasing a volume again

`unmanage` removes Cinder's record and **leaves the zvol in place** with its
data intact:

```bash
cinder unmanage <volume>
cinder snapshot-unmanage <snapshot>
```

Nothing is deleted on the appliance. Cinder removes the iSCSI export it built,
and the zvol keeps the name Cinder gave it — so it can be adopted again with
`<pool>/volume-<uuid>` as the identifier.

This is the rollback. If an adoption turns out to be wrong, unmanage it; the
disk is exactly where it was, under a different name.

Delete the zvol by hand if you genuinely want it gone. Nothing else will.

## Order for a whole estate

1. Adopt one disk end to end — manage, attach, boot, unmanage — and confirm the
   zvol survives. Do this on something disposable.
2. Decide `truenas_adopt_removes_export`. Migrating disks one at a time with it
   off is safest; a batch with it off means a manual cleanup per disk.
3. Shut down each machine, adopt its disk, boot it under Nova, confirm, move on.
4. Adopt snapshots afterwards, only where they are worth keeping in Cinder.

Adoption is per-disk and independently reversible, so there is no window in
which the estate is half-migrated and unrecoverable. That is the property worth
protecting: prefer many small reversible steps over one large one.
