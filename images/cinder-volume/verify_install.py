"""Fail the image build if the driver is not usable inside it.

Run as a build step. The point is that a broken image fails at `docker
build` rather than as a crash-looping `cinder-volume` container after the
deployment has already been rolled out.
"""

import sys

REQUESTS_FLOOR = (2, 31)


def main():
    try:
        import requests
    except ImportError:
        sys.exit(
            "requests is not present in the Kolla venv. This driver needs "
            "it and deliberately does not install it, to avoid moving "
            "Cinder's own dependencies underneath it. Install it "
            "explicitly, pinned, and rebuild."
        )

    have = tuple(int(part) for part in requests.__version__.split(".")[:2])
    if have < REQUESTS_FLOOR:
        sys.exit(
            "requests %s in the base image is older than the %s this driver "
            "requires." % (requests.__version__,
                           ".".join(str(p) for p in REQUESTS_FLOOR))
        )

    # The check that matters: pip put the package somewhere, but only an
    # import proves this interpreter -- the one cinder-volume runs -- can
    # find it.
    from truenas_cinder_driver import __version__
    from truenas_cinder_driver.driver import TrueNASISCSIDriver

    if TrueNASISCSIDriver.VERSION != __version__:
        sys.exit(
            "driver version %s does not match package version %s; the "
            "single-source-of-truth wiring is broken."
            % (TrueNASISCSIDriver.VERSION, __version__)
        )

    print("TrueNAS driver %s imports OK (requests %s, python %s)"
          % (__version__, requests.__version__,
             ".".join(str(p) for p in sys.version_info[:3])))


if __name__ == "__main__":
    main()
