"""
TrueNAS Cinder Driver package.

This package implements a Cinder volume driver for TrueNAS Scale over
iSCSI: zvols as volumes, with the iSCSI export built and torn down per
attach.

**iSCSI only.** There is no NFS support and none is planned for v1. An
earlier version of this docstring claimed both protocols; nothing ever
implemented NFS.
"""

# Single source of truth. pyproject.toml reads this attribute for the
# package version, and driver.TrueNASISCSIDriver.VERSION reports it to
# Cinder, so the two cannot drift.
__version__ = "1.0.0"

from truenas_cinder_driver.api_client import (
    TrueNASAPIClient,
    TrueNASAPIError,
    TrueNASAPIAuthError,
    TrueNASAPIConnectionError,
    TrueNASAPINotFoundError,
    TrueNASAPITimeoutError,
)

__all__ = [
    "TrueNASAPIClient",
    "TrueNASAPIError",
    "TrueNASAPIAuthError",
    "TrueNASAPIConnectionError",
    "TrueNASAPINotFoundError",
    "TrueNASAPITimeoutError",
    "__version__",
]
