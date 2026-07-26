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
3.  Install development dependencies: `pip install -r test-requirements.txt`.

## Code Style

- Follow PEP 8 style guidelines.
- Use meaningful variable and function names.
- Add docstrings to all public functions and classes.

## Testing

- Write unit tests for new features or bug fixes.
- Run existing tests before submitting a pull request: `tox -e py310`.

## Documentation

- Update the README and other documentation as needed.
- Ensure all public APIs are well-documented.

## Getting Help

If you have questions or need help, please open an issue on GitHub.