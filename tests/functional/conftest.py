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

Some calls here go through `client._make_request` rather than a public
method, in two distinct cases (#44):

**Asserting a wrong form is still wrong.** The leaf-name rename, the
unforced rename, the empty-body promote. These *must* bypass the client,
because the client always sends the correct form -- routing them through a
public method would make them untestable.

**Removing something the driver never removes.** Portals, initiator groups
and stopping the iSCSI service. The driver creates a portal never, an
initiator group without ever deleting one, and starts the service without
stopping it -- so there is no `delete_portal`, `delete_initiator_group` or
`stop_iscsi_service` to call. Adding them for the tests' benefit would put
surface in the shipped client that nothing in production exercises, which
is a worse trade than a documented raw call in a test.

#48's reconciliation confirmed the boundary: everything it can remediate
(targets, extents, links) already has a client method, because those are
the things the driver itself creates and destroys.
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

    _destroy_zvol(client, pool, names.zvol)


def _destroy_zvol(client, pool, name):
    """Remove a zvol and anything ZFS will not let it be removed with.

    Snapshots first: ZFS refuses to destroy a dataset that still has
    them, and this suite deliberately never passes `recursive=True` -- a
    cleanup path that cascades is one that can delete something a test did
    not create.
    """
    for snapshot in client.get_snapshot_list(f"{pool}/{name}") or []:
        _quietly(client.delete_snapshot, snapshot["id"])
    _quietly(client.delete_zvol, pool, name)


@pytest.fixture
def destroy_zvol(client):
    """The teardown a test can register for a zvol it created itself.

    Same logic the `zvol` fixture uses. Registering a bare `delete_zvol`
    instead leaves a zvol stranded the moment a test snapshots it, which
    is how this fixture came to exist.
    """
    def destroy(pool, name):
        _destroy_zvol(client, pool, name)
    return destroy


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
def owned_initiator_groups(client, names, cleanup):
    """Remove the initiator groups this test's exports create, last.

    The driver creates groups and never removes one -- another volume may
    still be attached through it, and there is no lifecycle event that
    says otherwise -- so the suite owns them.

    Registered as a fixture rather than inside a test body so it runs
    *after* every export the test registered: a group is still referenced
    by any target built on it, and `cleanup` unwinds in reverse
    registration order. Deleting it first asks the appliance to remove
    something in use.

    Matched by IQN rather than by id because the group under test may not
    exist yet when this registers -- concurrent exports are what create
    it, and how many of them there are is the thing being asserted.
    """
    def drop():
        for group in client.get_initiator_groups():
            if group.get("initiators") == [names.iqn]:
                client._make_request(
                    "DELETE", "/iscsi/initiator/id/%s" % group["id"])

    cleanup("initiator groups holding %s" % names.iqn, drop)


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


# --- driver-level fixtures ------------------------------------------------
#
# Skipped wholesale when Cinder is absent, so the client-level suite still
# runs in the dependency-free environment.

class _Cfg(object):
    def __init__(self, **kw):
        self._v = dict(kw)

    def append_config_values(self, opts):
        for opt in opts:
            self._v.setdefault(opt.name, opt.default)

    def safe_get(self, name):
        return self._v.get(name)

    def __getattr__(self, name):
        try:
            return self.__dict__["_v"][name]
        except KeyError:
            raise AttributeError(name)


class _Volume(object):
    def __init__(self, name, size=1):
        self.name = name
        self.size = size


@pytest.fixture(scope="session")
def coordinator():
    """A started tooz coordinator, on a throwaway file backend.

    The driver takes locks (#18), and `COORDINATOR.get_lock` raises
    `LockCreationFailed` when the coordinator has not been started. Cinder
    starts one in the service, so anything driving the driver outside a
    service has to as well -- including this suite. Session-scoped because
    it is process-global state either way.
    """
    import tempfile

    from cinder import coordination
    from oslo_config import cfg

    state = tempfile.mkdtemp(prefix="cinder-func-lock-")
    cfg.CONF.set_override("backend_url", "file://%s" % state,
                          group="coordination")
    coordination.COORDINATOR.start()
    yield
    coordination.COORDINATOR.stop()


@pytest.fixture
def driver(config, pool, request, portals, portal_addresses, iscsi_service,
           coordinator):
    """A driver whose setup validation has run against the appliance.

    Depends on `portals` and `iscsi_service` because
    `check_for_setup_error` requires both -- a fresh appliance has zero
    portals and a STOPPED service, and without provisioning them this
    module would error at fixture setup rather than run.

    `truenas_iscsi_portal_id` is set explicitly rather than left to
    discovery: the driver refuses to guess when an appliance has several
    portals, and a shared appliance may well have several.
    """
    # Imported here, not at module scope: conftest is loaded for the
    # client-level suite too, which runs without Cinder installed.
    from truenas_cinder_driver import driver as tnd

    url, key, _pool, verify_ssl = config
    adopt_removes = getattr(request, "param", False)
    cfg = _Cfg(truenas_api_url=url, truenas_api_key=key, truenas_pool=pool,
               truenas_verify_ssl=verify_ssl,
               truenas_iscsi_portal_id=portals[0],
               truenas_iscsi_portal_addresses=portal_addresses,
               truenas_adopt_removes_export=adopt_removes,
               volume_backend_name="truenas-iscsi", san_is_local=False)
    d = tnd.TrueNASISCSIDriver(configuration=cfg)
    d.do_setup(None)
    # Not merely setup: this is the check that fails loudly when the
    # appliance is misconfigured, and it has to pass before anything below
    # means anything.
    d.check_for_setup_error()
    return d
