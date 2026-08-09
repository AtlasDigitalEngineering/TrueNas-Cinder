"""
TrueNAS Cinder Driver package.

This package implements a Cinder volume driver for TrueNAS Scale,
supporting both iSCSI and NFS storage protocols.
"""

__version__ = "0.1.0"

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
