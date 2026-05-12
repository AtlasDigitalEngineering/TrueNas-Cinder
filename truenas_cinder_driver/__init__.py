"""
TrueNAS Cinder Driver package.

This package implements a Cinder volume driver for TrueNAS Scale,
supporting both iSCSI and NFS storage protocols.
"""

__version__ = "0.1.0"

from truenas_cinder_driver.api_client import TrueNASClient

__all__ = ["TrueNASClient", "__version__"]