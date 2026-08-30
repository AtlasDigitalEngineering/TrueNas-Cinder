# TrueNAS Cinder Driver

A modern, OpenStack-compliant Cinder driver for TrueNAS Scale.

## Overview

This project provides a Cinder volume driver that enables OpenStack to use TrueNAS Scale as a backend storage system. The driver supports both iSCSI and NFS protocols, with primary focus on iSCSI for VM block storage.

## Features

- **OpenStack Compliance**: Fully compatible with the Cinder API for seamless integration.
- **TrueNAS Scale Integration**: Leverages the modern REST API for robust management of zvols and iSCSI targets.
- **Flexible Backend Support**: Supports both iSCSI (primary) and NFS storage protocols.

## Repository Structure

```
TrueNas-Cinder/
├── .github/                # CI workflows and CODEOWNERS
├── docs/
│   ├── PLANNING.md         # Project roadmap and milestones
│   ├── configuration.md    # cinder.conf backend section + prerequisites
│   └── deployment.md       # Building the image and deploying under Kolla
├── images/
│   └── cinder-volume/      # Dockerfile for the Kolla cinder-volume image
├── truenas_cinder_driver/
│   ├── __init__.py         # Package initialization
│   ├── api_client.py       # TrueNAS REST API client wrapper
│   └── driver.py           # Cinder volume driver
├── tests/
│   ├── unit/               # API client tests (no Cinder needed)
│   └── driver/             # Driver tests (Cinder required)
├── pyproject.toml          # Packaging and dependencies (extras)
├── flake.nix               # Nix dev shell (Python, uv, gh)
├── AGENTS.md               # Conventions and workflow for contributors
└── CONTRIBUTING.md         # Contribution guidelines
```

`driver.py` provides configuration, setup validation, capacity reporting, the
volume lifecycle and the iSCSI export/connection path. Snapshots, clones,
extend and the `manage_existing` family are still in progress — see the open
issues.

See [docs/configuration.md](docs/configuration.md) for a sample `cinder.conf`
backend section and the appliance prerequisites the driver validates at
startup.

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

The two suites are split so the API client tests stay fast and dependency-free;
Cinder pulls in around 58 packages. The package is not installable yet, so use
`python -m pytest` rather than bare `pytest` — see [AGENTS.md](AGENTS.md) for
the detail, including the extra step needed on NixOS.

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
