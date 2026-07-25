"""
API client for TrueNAS Scale REST API.

This module provides a robust interface to interact with the TrueNAS Scale
REST API, handling authentication and error responses.
"""

import requests
from typing import Dict, Any, Optional, List
from urllib.parse import quote


# TrueNAS reports and accepts volsize in bytes; Cinder works in GB.
GIB = 1024 ** 3


class TrueNASClient:
    """Client for TrueNAS Scale REST API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 443,
        verify_ssl: bool = False
    ):
        """
        Initialize the TrueNAS client.

        Args:
            host: TrueNAS host address (e.g., 'truenas.example.com')
            username: Username for authentication
            password: Password for authentication
            port: API port (default 443)
            verify_ssl: Whether to verify SSL certificates (default False)
        """
        self.base_url = f"https://{host}:{port}/api/v2.0"
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify_ssl

    def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Make a request to the TrueNAS API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path (without /api/v2.0)
            **kwargs: Additional arguments to pass to requests

        Returns:
            JSON response as dictionary
        """
        url = f"{self.base_url}{endpoint}"
        response = self.session.request(method, url, **kwargs)

        # Raise an exception for bad status codes
        response.raise_for_status()

        return response.json()

    def is_eula_accepted(self) -> bool:
        """
        Check if the TrueNAS End-User License Agreement (EULA) is accepted.

        Returns:
            True if EULA is accepted, False otherwise
        """
        data = self._make_request("GET", "/truenas/is_eula_accepted")
        return bool(data.get("accepted", False))

    def get_pool_list(self) -> List[Dict[str, Any]]:
        """
        Get list of available storage pools.

        Returns:
            List of pool information dictionaries
        """
        return self._make_request("GET", "/pool")

    @staticmethod
    def _dataset_id(pool: str, name: str) -> str:
        """
        Build the URL-encoded dataset identifier for a zvol.

        TrueNAS addresses datasets by their full ZFS path, with the separator
        percent-encoded: ``tank/vol1`` becomes ``tank%2Fvol1``. Nested names
        encode every separator, so ``tank`` + ``proxmox/vm-100`` becomes
        ``tank%2Fproxmox%2Fvm-100``.

        Args:
            pool: Pool name
            name: Zvol name, which may itself contain '/'

        Returns:
            Percent-encoded dataset identifier
        """
        return quote(f"{pool}/{name}", safe="")

    def create_zvol(
        self,
        pool: str,
        name: str,
        size_gb: int,
        sparse: bool = True,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new zvol (block device).

        Args:
            pool: Name of the pool to create the zvol in
            name: Name for the zvol
            size_gb: Size of the zvol in GB
            sparse: Whether to thin-provision the zvol (default True)
            **kwargs: Additional dataset properties

        Returns:
            Information about the created zvol
        """
        payload = {
            "name": f"{pool}/{name}",
            "type": "VOLUME",
            "volsize": size_gb * GIB,
            "volmode": "GEOM",
            "sparse": sparse,
            **kwargs
        }
        return self._make_request("POST", "/pool/dataset", json=payload)

    def get_zvol(self, pool: str, name: str) -> Dict[str, Any]:
        """
        Get a single zvol by pool and name.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name

        Returns:
            Zvol metadata, including volsize
        """
        dataset_id = self._dataset_id(pool, name)
        return self._make_request("GET", f"/pool/dataset/id/{dataset_id}")

    def delete_zvol(
        self,
        pool: str,
        name: str,
        recursive: bool = False
    ) -> None:
        """
        Delete a zvol.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name
            recursive: Whether to delete dependent children (default False)
        """
        dataset_id = self._dataset_id(pool, name)
        self._make_request(
            "DELETE",
            f"/pool/dataset/id/{dataset_id}",
            json={"recursive": recursive},
        )

    def resize_zvol(
        self,
        pool: str,
        name: str,
        new_size_gb: int
    ) -> Dict[str, Any]:
        """
        Resize an existing zvol.

        ZFS supports online growth, so no iSCSI reconnect is required. This
        does not shrink a zvol -- ZFS rejects a volsize below current usage.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name
            new_size_gb: New size in GB

        Returns:
            Updated zvol metadata
        """
        dataset_id = self._dataset_id(pool, name)
        return self._make_request(
            "PUT",
            f"/pool/dataset/id/{dataset_id}",
            json={"volsize": new_size_gb * GIB},
        )

    def list_zvols(self, pool: str) -> List[Dict[str, Any]]:
        """
        List every zvol in a pool.

        Args:
            pool: Pool to list zvols from

        Returns:
            List of zvol metadata dictionaries
        """
        return self._make_request(
            "GET",
            "/pool/dataset",
            params={"type": "VOLUME", "name__startswith": f"{pool}/"},
        )

    def get_iscsi_target_list(self) -> List[Dict[str, Any]]:
        """
        Get list of iSCSI targets.

        Returns:
            List of iSCSI target information dictionaries
        """
        return self._make_request("GET", "/iscsi/target")

    def create_iscsi_target(
        self,
        name: str,
        alias: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new iSCSI target.

        Args:
            name: Name for the iSCSI target
            alias: Optional alias for the target
            **kwargs: Additional target properties

        Returns:
            Information about the created target
        """
        payload = {
            "name": name,
            "alias": alias,
            **kwargs
        }
        return self._make_request("POST", "/iscsi/target", json=payload)

    def delete_iscsi_target(self, id: int) -> None:
        """
        Delete an iSCSI target by ID.

        Args:
            id: Target ID to delete
        """
        self._make_request("DELETE", f"/iscsi/target/id/{id}")

    def create_iscsi_extent(
        self,
        name: str,
        path: str,
        disk_type: str = "Disk",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new iSCSI extent (maps a zvol to an iSCSI target).

        Args:
            name: Name for the extent
            path: Path to the zvol (e.g., "/dev/zvol/tank/volume1")
            disk_type: Type of disk (default "Disk")
            **kwargs: Additional extent properties

        Returns:
            Information about the created extent
        """
        payload = {
            "name": name,
            "type": disk_type,
            "path": path,
            **kwargs
        }
        return self._make_request("POST", "/iscsi/extent", json=payload)

    def delete_iscsi_extent(self, id: int) -> None:
        """
        Delete an iSCSI extent by ID.

        Args:
            id: Extent ID to delete
        """
        self._make_request("DELETE", f"/iscsi/extent/id/{id}")

    def create_iscsi_target_extent(
        self,
        target_id: int,
        extent_id: int,
        lun_id: int = 0
    ) -> Dict[str, Any]:
        """
        Associate an iSCSI extent with a target.

        Args:
            target_id: ID of the iSCSI target
            extent_id: ID of the iSCSI extent
            lun_id: LUN number (default 0)

        Returns:
            Information about the created association
        """
        payload = {
            "target": target_id,
            "extent": extent_id,
            "lunid": lun_id
        }
        return self._make_request("POST", "/iscsi/targetextent", json=payload)

    def delete_iscsi_target_extent(self, id: int) -> None:
        """
        Delete an iSCSI target-extent association by ID.

        Args:
            id: Target-extent association ID to delete
        """
        self._make_request("DELETE", f"/iscsi/targetextent/id/{id}")

    def get_snapshot_list(self) -> List[Dict[str, Any]]:
        """
        Get list of snapshots.

        Returns:
            List of snapshot information dictionaries
        """
        return self._make_request("GET", "/zfs/snapshot")

    def create_snapshot(
        self,
        dataset: str,
        name: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new snapshot.

        Args:
            dataset: Dataset name (e.g., "tank/volume1")
            name: Snapshot name
            **kwargs: Additional snapshot options

        Returns:
            Information about the created snapshot
        """
        payload = {
            "dataset": dataset,
            "name": name,
            **kwargs
        }
        return self._make_request("POST", "/zfs/snapshot", json=payload)

    def delete_snapshot(self, id: str) -> None:
        """
        Delete a snapshot by ID.

        Args:
            id: Snapshot ID to delete
        """
        self._make_request("DELETE", f"/zfs/snapshot/id/{id}")
