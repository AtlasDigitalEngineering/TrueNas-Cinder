# TrueNAS Cinder Driver

A modern, OpenStack-compliant Cinder driver for TrueNAS Scale.

## Overview

A Cinder volume driver that lets OpenStack use TrueNAS Scale as block storage
**over iSCSI**. Each Cinder volume is a zvol; the iSCSI export is built and torn
down around each attach.

It exists to migrate an estate of Proxmox-created zvols to OpenStack without
copying them — `cinder manage` renames a zvol in place, so a 10 TiB disk is
adopted as fast as a 1 GiB one.

## Features

- **The full volume lifecycle** — create, delete, attach, detach, extend,
  snapshot, clone, and clone-from-snapshot.
- **Adoption of existing zvols** — `manage_existing` and its snapshot
  equivalent, with a safety gate that refuses a zvol holding a live iSCSI
  session and explains what to remove.
- **Multipath** — one target bound to every configured portal address,
  presented to Cinder as `target_portals` / `target_iqns` / `target_luns`.
- **Verified against real hardware** — a functional suite runs the client and
  the driver against a live appliance, including a real iSCSI login.

**iSCSI only.** There is no NFS support and none is planned for v1. Multi-attach
and CHAP are deferred (#27).

## Repository Structure

```
TrueNas-Cinder/
├── .github/                # CI workflows and CODEOWNERS
├── docs/
│   ├── PLANNING.md         # Why the project exists and what each milestone means
│   ├── configuration.md    # cinder.conf backend section + prerequisites
│   ├── deployment.md       # Building the image and deploying under Kolla
│   ├── migration.md        # Adopting existing zvols into Cinder
│   ├── troubleshooting.md  # Symptoms this driver has actually produced
│   └── api-reference.md    # TrueNAS endpoints used, and their surprises
├── images/
│   └── cinder-volume/      # Dockerfile for the Kolla cinder-volume image
├── truenas_cinder_driver/
│   ├── __init__.py         # Exports the client, the exceptions and __version__
│   ├── api_client.py       # TrueNAS REST API client wrapper
│   ├── driver.py           # Cinder volume driver
│   └── reconcile.py        # Finds appliance objects with no Cinder volume
├── tools/
│   ├── find_orphans.py     # Reconciliation CLI (reads .env and OS_*)
│   └── check_workflows.py  # Refuses `${{ }}` inside a workflow `run:` body
├── tests/
│   ├── unit/               # No Cinder, no appliance; runs on `requests` alone
│   ├── driver/             # Driver tests (Cinder required)
│   ├── functional/         # Live appliance; skipped unless .env configures one
│   └── fixtures/           # Inputs the CI jobs lint against
├── pyproject.toml          # Packaging and dependencies (extras)
├── flake.nix               # Nix dev shell (Python, uv, gh)
├── AGENTS.md               # Conventions and workflow for contributors
└── CONTRIBUTING.md         # Contribution guidelines
```

Status — what is merged, in progress or blocked — lives in the GitHub issues
and their milestones, not here, so that this file cannot go quietly out of
date.

## Documentation

| | |
|---|---|
| [configuration.md](docs/configuration.md) | Sample `cinder.conf`, every option, and the prerequisites the driver checks at startup |
| [deployment.md](docs/deployment.md) | Building the image and deploying under Kolla-Ansible |
| [migration.md](docs/migration.md) | Adopting existing zvols into Cinder, one disk at a time |
| [troubleshooting.md](docs/troubleshooting.md) | **Start here when something breaks** — symptoms, causes and remedies |
| [api-reference.md](docs/api-reference.md) | The TrueNAS endpoints used, and the behaviours that are not what the API docs imply |

## Development

### Prerequisites

- Python 3.12 — the deployment target, from Kolla 2025.1's Ubuntu Noble 24.04
  base. CI also runs the API client suite on 3.10 for breadth
- TrueNAS Scale (v24.x or later)
- OpenStack Cinder — provided by the deployment at runtime. Not needed for the
  API client tests; required for the driver tests, which import it

### Setup

With Nix — the API client tests, linter and verification tool need no setup at
all; only the driver tests, which need Cinder, install anything:

```bash
nix develop                            # ready immediately
uv venv && uv pip install -e '.[driver]'   # driver tests only
```

Without Nix:

1. Clone the repository.
2. Create a virtual environment: `python -m venv .venv && source .venv/bin/activate`
3. Install the package and its test dependencies: `pip install -e '.[test]'`
4. For the driver tests as well: `pip install -e '.[driver]'`

### Running the tests

```bash
python -m pytest tests/unit             # API client — runs on `requests` alone
.venv/bin/python -m pytest tests/driver # Driver — needs Cinder installed
```

There is a third suite, `tests/functional`, which talks to a real appliance. It
**skips entirely** unless one is configured in `.env`, so it never runs by
accident — see [CONTRIBUTING.md](CONTRIBUTING.md).

The suites are split so the API client tests stay fast and dependency-free;
Cinder pulls in around 58 packages. Use `python -m pytest` rather than bare
`pytest`, so the working tree is tested ahead of any installed copy — see
[AGENTS.md](AGENTS.md) for the detail, including the extra step needed on
NixOS.

## Roadmap

See [PLANNING.md](docs/PLANNING.md) for why the project exists, how it is
structured, and what each milestone means. Current status — what is merged, in
progress or blocked — lives in the GitHub issues and their milestones:

- **M1 — Minimum viable attach**: create, delete, attach and detach, proven by a
  Nova instance booting from a TrueNAS-backed volume.
- **M2 — Full lifecycle**: snapshots, clones and extend, with rollback paths
  covered.
- **M3 — Migration ready**: `manage_existing` validated against a real
  Proxmox-created zvol, and a Kolla image deployed.

## Contributing

We welcome contributions! Please read our [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the GPL-3.0 License - see the LICENSE file for details.
