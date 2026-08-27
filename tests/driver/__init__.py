"""Unit tests that require Cinder to be installed.

Kept apart from ``tests/unit`` on purpose. Those tests run against
``requests`` alone, which keeps the common CI job fast and dependency-free;
importing anything under ``truenas_cinder_driver.driver`` pulls in Cinder
and its ~58 dependencies, so those tests get their own tox env and CI job.

Discovery must therefore be scoped -- ``discover -s tests`` would pick these
up and fail on a machine without Cinder. Use ``-s tests/unit`` or
``-s tests/driver``.
"""
