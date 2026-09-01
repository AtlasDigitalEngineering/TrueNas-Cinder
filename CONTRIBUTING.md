# Contributing to TrueNAS Cinder Driver

Thank you for your interest in contributing to the TrueNAS Cinder Driver! This document provides guidelines and instructions for contributing.

## Code of Conduct

By participating in this project, you agree to abide by the code of conduct.

## How Can I Contribute?

### Reporting Bugs

- Use GitHub issues to report bugs.
- Include steps to reproduce, expected behavior, and actual behavior.
- Provide system information (TrueNAS version, OpenStack version).

### Suggesting Enhancements

- Open a GitHub issue with a clear description of the enhancement.
- Explain why this feature would be useful to most users.

### Pull Requests

1.  Fork the repository.
2.  Create a new branch for your feature or fix (`git checkout -b feature/amazing-feature`).
3.  Make your changes and commit them with a descriptive message.
4.  Push your branch (`git push origin feature/amazing-feature`).
5.  Open a pull request against the `main` branch.

## Development Setup

1.  Clone the repository.
2.  Create a virtual environment.
3.  Install the package and its development dependencies:
    `pip install -e '.[test]'`, or `pip install -e '.[driver]'` to
    include Cinder for the driver tests.

## Code Style

- Follow PEP 8 style guidelines.
- Use meaningful variable and function names.
- Add docstrings to all public functions and classes.

## Testing

- Write unit tests for new features or bug fixes.
- Run existing tests before submitting a pull request: `tox` runs
  `py312`, `driver` and `flake8`. The functional suite is opt-in;
  see below.

### Functional tests against a real appliance

`tests/functional/` talks to an actual TrueNAS box. It **skips entirely**
unless one is configured, so it never runs by accident:

```bash
cp .env.example .env      # then fill it in
tox -e functional
# or: python -m pytest tests/functional
```

These tests create and destroy datasets, snapshots and iSCSI exports. Point
them at a scratch pool on a development appliance and nothing else. Never at
a production system — it holds disks whose only copy is on that appliance.

#### Standing up a target

1. **A TrueNAS Scale appliance** you are willing to have datasets created and
   destroyed on. A VM is fine; the suite creates 1 GiB sparse zvols and
   removes them.
2. **A scratch pool.** Any pool works, but give it a name you will recognise
   as disposable — `Dev-Pool`, `Scratch-Pool`. It goes in `TRUENAS_TEST_POOL`,
   which is deliberately not the same variable the driver's own config uses,
   so a stray export cannot aim the suite at a live pool.
3. **An API key** — *Credentials → Local Users → root → API Keys*. The suite
   creates and deletes datasets and iSCSI objects, so a read-only key will
   fail partway through rather than skipping.
4. **Optionally a second static IP** on the appliance. Multipath assertions
   need a portal bound to more than one address; with a single-homed box
   those tests skip and say so.

The suite does not need an initiator, a portal or an iSCSI service configured
in advance. It reuses a portal if one exists and creates one otherwise, and it
starts `iscsitarget` if it is stopped, putting both back as it found them.

That includes the tests that log in. Some of them establish a genuine iSCSI
session against the target the driver built, but they do it by speaking the
login exchange over a socket rather than through the kernel — so **no `root`,
no `open-iscsi` and no `iscsi_tcp` module**, and nothing persistent: a session
is a TCP connection, and there is no node database to leave stale entries in.

It does not require an *idle* appliance either: every assertion is scoped to
objects the test created, so it passes on a box that already has exports on it.
That half is verified — it is run routinely against a development appliance
hosting a live Cinder deployment.

**The fresh-appliance path is implemented but not yet exercised.** Creating a
portal and starting a stopped service are the branches a brand-new box would
take, and they have not been run: the only appliance available for testing
hosts a deployment with an attached volume, so deleting its portal or stopping
its iSCSI service to reach those branches would break something real. If you
run this against a genuinely clean appliance, that is new information — please
say whether it worked.

A full run takes about five minutes.

## Documentation

- Update the README and other documentation as needed.
- Ensure all public APIs are well-documented.

## Getting Help

If you have questions or need help, please open an issue on GitHub.