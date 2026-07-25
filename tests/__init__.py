"""Test package for the TrueNAS Cinder driver.

Present so `python -m unittest discover -s tests -t .` can import the tree.
It also makes the repo root the package basedir under pytest's default
`prepend` import mode, so `truenas_cinder_driver` resolves without the
package being installed.
"""
