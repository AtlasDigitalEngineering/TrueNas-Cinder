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
│   └── PLANNING.md         # Project roadmap and milestones
├── truenas_cinder_driver/
│   ├── __init__.py         # Package initialization
│   └── api_client.py       # TrueNAS REST API client wrapper
├── tests/
│   └── unit/               # Unit tests
├── AGENTS.md               # Conventions and workflow for contributors
└── CONTRIBUTING.md         # Contribution guidelines
```

The Cinder driver itself (`driver.py`) is not implemented yet — see the
roadmap below and the open issues.

## Development

### Prerequisites

- Python 3.10+ — 3.10 is the deployment target (Kolla 2025.1 / Ubuntu Jammy);
  CI also runs 3.12
- TrueNAS Scale (v24.x or later)
- OpenStack Cinder (provided by the deployment; not installed for local
  development or testing)

### Setup

1. Clone the repository.
2. Create a virtual environment: `python -m venv .venv && source .venv/bin/activate`
3. Install test dependencies: `pip install -r test-requirements.txt`

### Running the tests

```bash
python -m pytest tests/unit
```

The package is not installable yet, so use `python -m pytest` rather than bare
`pytest` — see [AGENTS.md](AGENTS.md) for the detail.

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
