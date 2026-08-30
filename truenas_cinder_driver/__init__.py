"""
TrueNAS Cinder Driver package.

This package implements a Cinder volume driver for TrueNAS Scale,
supporting both iSCSI and NFS storage protocols.
"""

# Single source of truth. pyproject.toml reads this attribute for the
# package version, and driver.TrueNASISCSIDriver.VERSION reports it to
# Cinder, so the two cannot drift.
__version__ = "1.0.0rc1"

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
