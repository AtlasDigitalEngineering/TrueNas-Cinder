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
├── docs/               # Project documentation and planning
├── truenas_cinder_driver/
│   ├── __init__.py     # Package initialization
│   ├── driver.py       # Core Cinder driver implementation
│   └── api_client.py   # TrueNAS REST API client wrapper
├── tests/              # Unit and integration tests
├── CONTRIBUTING.md     # Contribution guidelines
└── PLANNING.md         # Project roadmap and milestones
```

## Development

### Prerequisites

- Python 3.8+
- TrueNAS Scale (v24.x or later)
- OpenStack Cinder

### Setup

1. Clone the repository.
2. Create a virtual environment: `python -m venv .venv && source .venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`

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

This project is licensed under the MIT License - see the LICENSE file for details.