# Deploying the driver under Kolla-Ansible

Kolla runs Cinder as containers, so an out-of-tree driver has to be **inside**
the `cinder-volume` image. There is no drop-in path — dropping a file onto the
host does nothing.

This repository owns the **image**. Your deployment configuration — inventory,
`globals.yml`, `passwords.yml`, which tag is running where — belongs in your own
deployment repository, not here.

## 1. Prerequisites

See [configuration.md](configuration.md) for what the appliance needs. The driver
validates all of it at startup and refuses to run otherwise, so getting it wrong
produces a clear error rather than a broken backend.

## 2. Build the image

```bash
uv build                                    # produces dist/*.whl
podman build -f images/cinder-volume/Dockerfile -t cinder-volume-truenas:1.0.0 .
```

`docker build` works identically. Build from the **repository root** — the
Dockerfile expects `dist/` and `images/cinder-volume/` in the context.

The base image is pinned by `ARG KOLLA_BASE` and can be overridden for a
different OpenStack release:

```bash
podman build --build-arg KOLLA_BASE=quay.io/openstack.kolla/cinder-volume:2025.1-ubuntu-noble ...
```

The build **fails** if the driver cannot be imported inside the image, rather
than letting `cinder-volume` crash-loop after it is deployed. It also checks
`requests` is present and new enough. The driver installs with `--no-deps`
deliberately: its only runtime dependency is `requests`, which Cinder already
provides, and a storage driver should not be able to upgrade a package inside
OpenStack's own virtualenv.

## 3. Publish it

Nodes need to pull the image from somewhere. Without a registry it has to reach
each node by hand (`podman save` / `podman load`), which is workable for one
development host and unworkable for several.

`ghcr.io` is the path of least resistance for a GitHub-hosted project: free for
public images, authenticated by the workflow's own `GITHUB_TOKEN`, and in the
same namespace as the repository. `.github/workflows/image.yml` publishes there
on a tag. Any OCI registry works — change `REGISTRY` in that workflow.

**Never deploy `:latest`.** Tag with the driver version, so a running container
can be traced back to a release.

## 4. Point Kolla at it

In your deployment's `globals.yml`:

```yaml
enable_cinder: "yes"
enable_cinder_backend_iscsi: "yes"
cinder_volume_image_full: "ghcr.io/<owner>/cinder-volume-truenas:1.0.0"
```

Multipath needs `enable_multipathd: "yes"` here, and
`volume_use_multipath = true` under `[libvirt]` in `nova.conf`, and the compute
nodes must be able to reach every portal address advertised. See
[configuration.md](configuration.md).

## 5. Give it the backend configuration

Kolla merges `/etc/kolla/config/cinder/cinder-volume.conf` on the deploy host
into the container's `cinder.conf`. The `[truenas-iscsi]` section from
[configuration.md](configuration.md) goes there. **This is the step most often
missed** — the image alone does nothing without it.

```ini
[DEFAULT]
enabled_backends = truenas-iscsi

[truenas-iscsi]
volume_driver = truenas_cinder_driver.driver.TrueNASISCSIDriver
volume_backend_name = truenas-iscsi
truenas_api_url = https://truenas.example.com
truenas_api_key = <service account API key>
truenas_pool = tank
```

`truenas_api_key` is `secret=True`, so oslo_config redacts it from logged
config dumps — but it is plain text in that file. Protect it accordingly.

## 6. Create the volume type

```bash
openstack volume type create truenas-iscsi \
  --property volume_backend_name=truenas-iscsi
```

Without it, volumes land wherever the scheduler chooses.

## 7. Check it came up

```bash
openstack volume service list
```

`cinder-volume` on `<host>@truenas-iscsi` should be `up`. If it is down, the
reason is in `cinder-volume.log` and will name the offending option and the
remedy — a stopped iSCSI service, a missing pool, an unprivileged API key, or a
portal bound to an address no compute node can reach.

Then prove it end to end rather than trusting the service state:

```bash
openstack volume create --type truenas-iscsi --size 1 smoke
openstack server add volume <instance> smoke
openstack server remove volume <instance> smoke
openstack volume delete smoke
```

The appliance should show a target and extent appear for the duration of the
attachment and disappear afterwards, leaving the zvol untouched until the
delete.

## Upgrading

Build a new image tagged with the new driver version, update
`cinder_volume_image_full`, and redeploy. Exports live on the appliance, not in
the container, so restarting `cinder-volume` does not disturb attached volumes —
teardown re-derives targets and extents by name rather than from anything the
process was holding.
