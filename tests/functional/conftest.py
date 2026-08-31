"""Fixtures for the functional suite (#25).

These tests talk to a **real TrueNAS appliance**. They are skipped unless
one is configured, so a normal `pytest tests/` run never touches a network.

Configuration comes from `.env`, the same file the
`tools/verify_endpoints.py` script used before this suite replaced
it:

    TRUENAS_API_URL=https://appliance.example.com
    TRUENAS_API_KEY=...
    TRUENAS_TEST_POOL=Scratch-Pool
    TRUENAS_VERIFY_SSL=1

`TRUENAS_TEST_POOL` is deliberately not `TRUENAS_POOL`. Pointing this suite
at the pool a deployment actually uses has to be a decision somebody typed,
not something a stray export can do to them -- these tests create and
destroy datasets.

Every fixture that creates something on the appliance removes it in
teardown, which pytest runs whether the test passed, failed or raised. That
is the main thing this suite gains over the script it replaces, whose
cleanup lived in `try/finally` blocks that a bad assertion could skip.
"""

import os
import pathlib
import time
from urllib.parse import urlsplit

import pytest

from truenas_cinder_driver import api_client


# Everything this suite creates carries this prefix, so a leak is
# attributable and so assertions can scope themselves to objects the test
# made. The suite runs against appliances that have other work on them --
# asserting a global list is empty is what made the old script fail on any
# appliance in use (#69).
PREFIX = "cinder-func"

ENV_FILE = pathlib.Path(__file__).resolve().parents[2] / ".env"


def _load_env():
    """Read `.env` into the environment, letting the file win.

    Deliberately not `setdefault`: a stale `TRUENAS_API_URL` left exported
    in a shell would otherwise silently outrank the file the operator just
    edited, and point a destructive run at a different appliance.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()


def _config():
    """Return the appliance settings, or None if not usable."""
    _load_env()
    url = os.environ.get("TRUENAS_API_URL")
    key = os.environ.get("TRUENAS_API_KEY")
    pool = os.environ.get("TRUENAS_TEST_POOL")
    if not (url and key and pool):
        return None
    # The example file ships placeholders. Running with them would produce
    # a confusing auth failure rather than an obvious "you did not fill
    # this in".
    if any("CHANGEME" in value for value in (url, key, pool)):
        return None
    return url, key, pool, os.environ.get("TRUENAS_VERIFY_SSL", "1") == "1"


@pytest.fixture(scope="session")
def config():
    """Appliance settings, skipping the whole suite when unconfigured."""
    settings = _config()
    if settings is None:
        pytest.skip(
            "no appliance configured: set TRUENAS_API_URL, TRUENAS_API_KEY "
            "and TRUENAS_TEST_POOL in .env to run the functional suite",
            allow_module_level=True,
        )
    return settings


@pytest.fixture(scope="session")
def client(config):
    """A client pointed at the configured appliance."""
    url, key, _pool, verify_ssl = config
    return api_client.TrueNASAPIClient(url, key, verify_ssl=verify_ssl)


@pytest.fixture(scope="session")
def pool(config):
    """The scratch pool these tests are allowed to write to."""
    return config[2]


@pytest.fixture
def names():
    """Unique, prefixed names for one test.

    Uniqueness matters more than tidiness: a previous run that died
    mid-teardown must not make the next one fail on a name collision, which
    would look like a product defect rather than leftover state.
    """
    stamp = "%s-%d" % (PREFIX, time.time_ns() // 1_000_000)

    class Names(object):
        base = stamp
        zvol = stamp
        clone = "%s-clone" % stamp
        snapshot = "%s-snap" % stamp
        target = "%s-tgt" % stamp
        extent = "%s-ext" % stamp
        iqn = "iqn.2005-03.org.open-iscsi:%s" % stamp

    return Names


@pytest.fixture
def zvol(client, pool, names):
    """A 1 GiB throwaway zvol, removed however the test ends.

    Teardown destroys any snapshots first: ZFS refuses to destroy a dataset
    that still has them, and this suite deliberately never passes
    `recursive=True` -- a cleanup path that cascades is one that can delete
    something a test did not create.
    """
    client.create_zvol(pool, names.zvol, size_gb=1)
    yield names.zvol

    for snapshot in client.get_snapshot_list(f"{pool}/{names.zvol}") or []:
        _quietly(client.delete_snapshot, snapshot["id"])
    _quietly(client.delete_zvol, pool, names.zvol)


def _quietly(fn, *args, **kwargs):
    """Run a teardown step, swallowing "already gone".

    Anything else is re-raised: a cleanup that cannot complete is a real
    finding, and silently ignoring it is how a suite starts leaking.
    """
    try:
        fn(*args, **kwargs)
    except api_client.TrueNASAPINotFoundError:
        pass


@pytest.fixture
def iscsi_service(client):
    """Ensure `iscsitarget` is running, and put it back afterwards.

    A fresh appliance has it STOPPED, and the driver's
    `check_for_setup_error` refuses to proceed without it -- deliberately,
    since a stopped service lets every export succeed and nothing attach.
    Tests that need the driver therefore have to start it, and owe the
    appliance its previous state back.
    """
    before = client.get_iscsi_service()["state"]
    if before != "RUNNING":
        client.start_iscsi_service()
    yield before

    if before != "RUNNING":
        client._make_request("POST", "/service/stop",
                             json={"service": "iscsitarget"})


@pytest.fixture
def portals(client, cleanup):
    """Portal ids to export through, reusing whatever the appliance has.

    A clean appliance has **zero** portals, and `create_target` rejects an
    empty list outright -- so a test that reads `get_portals()` raw fails
    with an unrelated `ValueError` on exactly the fresh box a new
    contributor is told to use. An appliance already in service, meanwhile,
    refuses a second portal on an address that already has one, so always
    creating does not work either.

    Hence: reuse if present, create if not, and only tidy up what this
    fixture made.

    Multipath needs a portal bound to more than one address, and a portal
    can only bind a *statically* configured one -- `listen_ip_choices`
    omits DHCP addresses entirely (#45). A single-homed appliance falls
    back to the wildcard and simply does not exercise multipath.
    """
    existing = client.get_portals()
    if existing:
        return [p["id"] for p in existing]

    choices = client._make_request(
        "GET", "/iscsi/portal/listen_ip_choices") or {}
    static = [ip for ip in choices if ip not in ("0.0.0.0", "::")]
    wanted = static[:2] if len(static) >= 2 else [None]

    created = []
    for ip in wanted:
        pid = client.create_portal(listen_ips=[ip] if ip else None,
                                   comment="cinder-func")
        created.append(pid)
        cleanup(f"portal {pid}", client._make_request,
                "DELETE", f"/iscsi/portal/id/{pid}")
    return created


@pytest.fixture
def portal_addresses(client, portals):
    """Addresses a compute node could actually reach the portal on.

    A portal bound to `0.0.0.0` reports an address no initiator can
    connect to, which `check_for_setup_error` rejects. That is the right
    behaviour and not something to work around -- so where the appliance
    offers only the wildcard, this supplies the host the tests are already
    talking to, which is precisely what an operator would configure.
    """
    listens = []
    for portal in client.get_portals():
        if portal["id"] in portals:
            listens += [entry["ip"] for entry in portal["listen"]]

    usable = [ip for ip in listens if ip not in ("0.0.0.0", "::")]
    if usable:
        return usable
    return [urlsplit(client.base_url).hostname]


@pytest.fixture
def cleanup(client):
    """Collect teardown callables, run in reverse order.

    For objects a test creates itself, where a dedicated fixture would be
    more ceremony than the test. Failures are reported rather than
    swallowed, because a leaked iSCSI target is exactly the kind of thing
    this suite exists to notice.
    """
    stack = []

    def add(label, fn, *args, **kwargs):
        stack.append((label, fn, args, kwargs))

    yield add

    failures = []
    for label, fn, args, kwargs in reversed(stack):
        try:
            fn(*args, **kwargs)
        except api_client.TrueNASAPINotFoundError:
            pass                       # already gone, or cascaded
        except Exception as exc:                          # noqa: BLE001
            failures.append("%s: %s: %s"
                            % (label, type(exc).__name__, exc))
    assert not failures, "teardown could not remove: %s" % "; ".join(failures)
