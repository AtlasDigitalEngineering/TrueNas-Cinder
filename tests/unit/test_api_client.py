"""
Unit tests for TrueNAS API client.

This module contains unit tests for the TrueNASClient class,
mocking HTTP requests to ensure proper functionality.
"""

import unittest
from unittest.mock import patch, MagicMock
from truenas_cinder_driver.api_client import TrueNASClient


class TestTrueNASClient(unittest.TestCase):
    """Test cases for TrueNASClient."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = TrueNASClient(
            host="truenas.example.com",
            username="admin",
            password="password123"
        )

    @patch('truenas_cinder_driver.api_client.requests.Session')
    def test_is_eula_accepted_true(self, mock_session):
        """Test is_eula_accepted returns True when EULA is accepted."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.json.return_value = {"accepted": True}
        mock_response.raise_for_status.return_value = None
        
        # Configure session mock
        mock_session_instance = MagicMock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        # Call method
        result = self.client.is_eula_accepted()

        # Verify results
        self.assertTrue(result)
        mock_session_instance.get.assert_called_once_with(
            "https://truenas.example.com:443/api/v2.0/truenas/is_eula_accepted"
        )

    @patch('truenas_cinder_driver.api_client.requests.Session')
    def test_is_eula_accepted_false(self, mock_session):
        """Test is_eula_accepted returns False when EULA is not accepted."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.json.return_value = {"accepted": False}
        mock_response.raise_for_status.return_value = None
        
        # Configure session mock
        mock_session_instance = MagicMock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        # Call method
        result = self.client.is_eula_accepted()

        # Verify results
        self.assertFalse(result)

    @patch('truenas_cinder_driver.api_client.requests.Session')
    def test_create_zvol(self, mock_session):
        """Test create_zvol creates a zvol with correct parameters."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "zvol/1",
            "name": "tank/volume1"
        }
        mock_response.raise_for_status.return_value = None
        
        # Configure session mock
        mock_session_instance = MagicMock()
        mock_session_instance.post.return_value = mock_response
        mock_session.return_value = mock_session_instance

        # Call method
        result = self.client.create_zvol(
            pool="tank",
            name="volume1",
            size_bytes=1073741824  # 1GB
        )

        # Verify results
        expected_payload = {
            "name": "tank/volume1",
            "volsize": 1073741824
        }
        
        mock_session_instance.post.assert_called_once_with(
            "https://truenas.example.com:443/api/v2.0/zfs/zvol",
            json=expected_payload
        )
        self.assertEqual(result, {"id": "zvol/1", "name": "tank/volume1"})

    @patch('truenas_cinder_driver.api_client.requests.Session')
    def test_get_pool_list(self, mock_session):
        """Test get_pool_list retrieves the list of pools."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"name": "tank", "size": 1073741824},
            {"name": "data", "size": 2147483648}
        ]
        mock_response.raise_for_status.return_value = None
        
        # Configure session mock
        mock_session_instance = MagicMock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        # Call method
        result = self.client.get_pool_list()

        # Verify results
        expected_url = "https://truenas.example.com:443/api/v2.0/pool"
        mock_session_instance.get.assert_called_once_with(expected_url)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "tank")

    @patch('truenas_cinder_driver.api_client.requests.Session')
    def test_create_iscsi_target(self, mock_session):
        """Test create_iscsi_target creates a target with correct parameters."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": 1,
            "name": "iqn.2005-10.org.freenas.ctl:volume1"
        }
        mock_response.raise_for_status.return_value = None
        
        # Configure session mock
        mock_session_instance = MagicMock()
        mock_session_instance.post.return_value = mock_response
        mock_session.return_value = mock_session_instance

        # Call method
        result = self.client.create_iscsi_target(
            name="iqn.2005-10.org.freenas.ctl:volume1"
        )

        # Verify results
        expected_payload = {
            "name": "iqn.2005-10.org.freenas.ctl:volume1",
            "alias": None
        }
        
        mock_session_instance.post.assert_called_once_with(
            "https://truenas.example.com:443/api/v2.0/iscsi/target",
            json=expected_payload
        )
        self.assertEqual(result["id"], 1)


if __name__ == '__main__':
    unittest.main()