"""
Unit tests package for TrueNAS Cinder Driver.
"""

import os

# Set test configuration environment variables
os.environ.setdefault('TRUENAS_HOST', 'test-truenas.example.com')
os.environ.setdefault('TRUENAS_USERNAME', 'testuser')
os.environ.setdefault('TRUENAS_PASSWORD', 'testpassword')