# cinder-volume with the TrueNAS driver installed.
#
# Kolla runs Cinder as containers, so an out-of-tree driver has to be inside
# the cinder-volume image; there is no drop-in path.
#
# Base image facts, read from the published image's own config blob on quay
# rather than assumed:
#
#   User        cinder
#   Entrypoint  dumb-init --single-child --
#   Cmd         kolla_start
#   PATH        /var/lib/kolla/venv/bin:...
#
# That PATH matters: OpenStack runs from a virtualenv at /var/lib/kolla/venv,
# so `pip` and `python` here are the venv's. Nothing below names a python
# version or a site-packages directory -- pip puts the package where this
# interpreter will find it, and verify_install.py proves it did.

ARG KOLLA_BASE=quay.io/openstack.kolla/cinder-volume:2025.1-ubuntu-noble
FROM ${KOLLA_BASE}

USER root

# Build from the repository root, after building the wheel:
#
#   uv build
#   docker build -f images/cinder-volume/Dockerfile -t <tag> .
COPY dist/truenas_cinder_driver-*.whl /tmp/
COPY images/cinder-volume/verify_install.py /tmp/

# --no-deps is deliberate. The driver's only runtime dependency is requests,
# which the base image already has because Cinder depends on it. Letting pip
# resolve dependencies here risks it upgrading a package inside OpenStack's
# own virtualenv to satisfy this driver -- a driver should not move Cinder's
# dependencies underneath it. verify_install.py checks the requirement
# instead, so an unsatisfied one fails the build and a human decides.
RUN set -eux; \
    pip install --no-cache-dir --no-deps /tmp/truenas_cinder_driver-*.whl; \
    python /tmp/verify_install.py; \
    rm -f /tmp/truenas_cinder_driver-*.whl /tmp/verify_install.py

USER cinder
