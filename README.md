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
│   └── configuration.md    # cinder.conf backend section + prerequisites
├── truenas_cinder_driver/
│   ├── __init__.py         # Package initialization
│   ├── api_client.py       # TrueNAS REST API client wrapper
│   └── driver.py           # Cinder volume driver
├── tests/
│   ├── unit/               # API client tests (no Cinder needed)
│   └── driver/             # Driver tests (Cinder required)
├── flake.nix               # Nix dev shell (Python, uv, gh)
├── AGENTS.md               # Conventions and workflow for contributors
└── CONTRIBUTING.md         # Contribution guidelines
```

`driver.py` currently provides configuration, setup validation and capacity
reporting. The volume lifecycle and the export/connection path are still in
progress — see the open issues.

See [docs/configuration.md](docs/configuration.md) for a sample `cinder.conf`
backend section and the appliance prerequisites the driver validates at
startup.

## Development

### Prerequisites

- Python 3.10+ — 3.10 is the deployment target (Kolla 2025.1 / Ubuntu Jammy);
  CI also runs 3.12
- TrueNAS Scale (v24.x or later)
- OpenStack Cinder — provided by the deployment at runtime. Not needed for the
  API client tests; required for the driver tests, which import it

### Setup

With Nix — the API client tests, linter and verification tool need no setup at
all; only the driver tests, which need Cinder, install anything:

```bash
nix develop                                       # ready immediately
uv venv && uv pip install -r driver-test-requirements.txt   # driver tests only
```

Without Nix:

1. Clone the repository.
2. Create a virtual environment: `python -m venv .venv && source .venv/bin/activate`
3. Install test dependencies: `pip install -r test-requirements.txt`
4. For the driver tests as well: `pip install -r driver-test-requirements.txt`

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

See [PLANNING.md](docs/PLANNING.md) for the detailed development roadmap.

1. Define project structure and architecture
2. Implement API client for TrueNAS Scale REST API
3. Develop core Cinder driver with OpenStack compliance
4. Establish testing framework (unit tests, CI/CD)
5. Create comprehensive documentation

## Contributing

We welcome contributions! Please read our [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the GPL-3.0 License - see the LICENSE file for details.
