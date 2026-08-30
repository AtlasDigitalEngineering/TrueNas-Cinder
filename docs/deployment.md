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

`ghcr.io` is the path of least resistance for a GitHub-hosted project:
authenticated by the workflow's own `GITHUB_TOKEN`, in the same namespace as
the repository, and free for public images. `.github/workflows/image.yml`
publishes there on a tag. Any OCI registry works — change `REGISTRY` in that
workflow.

**Never deploy `:latest`.** Tag with the driver version, so a running container
can be traced back to a release.

### Cutting a release

The image tag comes from `__version__` in `truenas_cinder_driver/__init__.py`,
never from the git tag. That is what stops the container and `pip show`
disagreeing, but it means **the git tag has to be bumped in lockstep** — a tag
of `v1.0.1` while `__version__` still says `1.0.0` would republish `:1.0.0`
over itself and attach a `1.0.0` wheel to a `v1.0.1` release.

The workflow refuses that rather than letting it through, so the order is:

1. Bump `__version__`, merge it.
2. Tag `main` as `v<__version__>` — exactly, including the `v`.
3. Push the tag.

A tag that disagrees fails the job before it can log in to the registry, and
says which of the two to change. A tag written without the `v` fails too,
rather than silently matching nothing and publishing no image at all.

### The first publish creates a private package

**Package visibility is separate from repository visibility, and publishing
does not link them.** A package first pushed with `GITHUB_TOKEN` is private
even when the repository is public, so the image will be there, the workflow
will be green, and every node will still fail to pull it with an
authentication error — at deploy time, after the Kolla configuration is
already done.

Changing it is a one-time manual step per package. It cannot be automated:
`GITHUB_TOKEN` does not carry the scope, and container visibility is not
exposed by the REST API at all.

> Repository → **Packages** → the package → **Package settings** → **Danger
> Zone** → **Change visibility** → **Public**

Then verify it **without credentials**, because a `docker pull` from a shell
that is already logged in proves nothing:

```bash
owner=atlasdigitalengineering
image=cinder-volume-truenas
tag=1.0.0

token=$(curl -s "https://ghcr.io/token?scope=repository:${owner}/${image}:pull" \
        | jq -r .token)
curl -sI -H "Authorization: Bearer ${token}" \
     -H "Accept: application/vnd.oci.image.index.v1+json,\
application/vnd.oci.image.manifest.v1+json,\
application/vnd.docker.distribution.manifest.list.v2+json,\
application/vnd.docker.distribution.manifest.v2+json" \
     "https://ghcr.io/v2/${owner}/${image}/manifests/${tag}" | head -1
```

**The `Accept` header is not optional.** Without it the registry declines to
serve the manifest and answers `404` even for an image that is public and
present — which reads exactly like failure and sends you back to the
visibility setting you just fixed correctly.

| Response | Meaning |
|---|---|
| `HTTP/2 200` | Public and present. Any node can pull it. |
| `HTTP/2 404` | Public, but no such tag. Check the tag, not the visibility. |
| `HTTP/2 401` | Still private. The token request fails first, so `${token}` is empty. |

If you would rather keep the package private, this is the point to configure
registry credentials on every node instead, which Kolla does through
`docker_registry_username` / `docker_registry_password` in `globals.yml`.

## 4. Point Kolla at it

In your deployment's `globals.yml`:

```yaml
enable_cinder: "yes"
enable_cinder_backend_iscsi: "yes"
cinder_volume_image_full: "ghcr.io/atlasdigitalengineering/cinder-volume-truenas:1.0.0"
```

The path is all-lowercase whatever the owner's login looks like — OCI
repository names have to be, so the publish workflow lowercases it. Copying the
mixed-case org name here gets `invalid reference format` from the pull.

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
