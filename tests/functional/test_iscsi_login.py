"""A real initiator logging in to what the driver exported (#90).

#25 wanted this and could not have it: the obvious route needs `root`,
`open-iscsi` and a kernel module, and leaves node records behind. The
`iscsi_probe` helper speaks the login exchange over a socket instead, so
these tests run as an ordinary user and leave nothing.

The value is in *where a failure lands*. Everything before this suite
asserts that the appliance was configured -- an extent exists, a target
names a portal, `provider_location` has the right shape. None of it
proves an initiator can get in, so a mistake in any of it surfaced as
"the instance did not boot", three layers away from its cause. These
tests fail as "the target refused this initiator", at the layer that
refused.

Both failure directions are asserted, not just the passing one: an
initiator outside the access list must be refused, and an address with
nothing on it must raise a connection error rather than a refusal. A
probe that cannot fail proves nothing about the login that passes.
"""

import pytest

pytest.importorskip("cinder", reason="driver tests need Cinder installed")

from tests.functional import iscsi_probe                     # noqa: E402
from tests.functional.conftest import _Volume                # noqa: E402


@pytest.fixture
def exported(driver, client, pool, zvol, names, cleanup,
             owned_initiator_groups):
    """A volume exported by the driver, torn down by the driver.

    Deliberately built through `create_export` rather than by assembling
    the pieces here: the point is to log in to what the *driver* built,
    including the `provider_location` it chose to hand back. A hand-made
    export would test this file's understanding of the appliance.
    """
    volume = _Volume(zvol)
    model = driver.create_export(None, volume, {'initiator': names.iqn})
    cleanup(f"export for {zvol}", driver.remove_export, None, volume)

    portals, iqn, lun = iscsi_probe.parse_provider_location(
        model['provider_location'])
    return {'volume': volume, 'model': model, 'portals': portals,
            'iqn': iqn, 'lun': lun}


def test_an_initiator_logs_in_with_what_the_driver_returned(
        client, names, exported):
    """The whole point: the export is usable, not merely present.

    Cross-checked against the appliance's own session list while the
    session is open. Two independent witnesses matter here -- this side
    parsing a status byte as success, and the appliance independently
    reporting a session for this IQN -- because a probe that misread the
    protocol could report a success that never happened.
    """
    address, port = exported['portals'][0]

    with iscsi_probe.login(address, names.iqn, exported['iqn'],
                           port=port) as session:
        assert session['tsih'], "target assigned no session handle"

        live = [s for s in client.get_iscsi_sessions()
                if s['initiator'] == names.iqn]
        assert live, ("logged in, but the appliance reports no session for "
                      f"{names.iqn}")
        assert live[0]['target'] == exported['iqn']

    assert not [s for s in client.get_iscsi_sessions()
                if s['initiator'] == names.iqn], "the session outlived logout"


def test_every_multipath_address_accepts_the_same_login(names, exported):
    """Each address in `provider_location` is a path, or it is a lie.

    `_provider_location` lists every address the portal binds, and the
    inherited `_get_iscsi_properties()` turns that list into
    `target_portals` for a multipath connector. Until now nothing checked
    that the second and later addresses are reachable at all -- #45
    asserted only that the appliance *reports* them.
    """
    if len(exported['portals']) < 2:
        pytest.skip("portal binds one address; multipath needs a "
                    "multi-homed appliance with static IPs (#45)")

    for address, port in exported['portals']:
        with iscsi_probe.login(address, names.iqn, exported['iqn'],
                               port=port) as session:
            assert session['tsih'], f"no session handle via {address}"


def test_an_initiator_outside_the_access_list_is_refused(names, exported):
    """The negative control for every test above.

    Without it, a probe that returned success unconditionally -- or an
    appliance that admitted anyone -- would pass the whole file. This is
    also the assertion that the initiator group is doing its job: the
    driver builds one per connector precisely so that a volume attached
    to one host is not reachable from another.
    """
    address, port = exported['portals'][0]

    with pytest.raises(iscsi_probe.LoginRefused) as caught:
        with iscsi_probe.login(address, names.iqn + '-stranger',
                               exported['iqn'], port=port):
            pass

    # Class 2 is "initiator error"; ctld answers both "no such target" and
    # "not in the access list" with detail 3, and does not distinguish
    # them on purpose -- telling an unauthorised initiator which targets
    # exist is exactly what an access list is for.
    assert caught.value.status_class == 2


def test_nothing_listening_is_a_connection_error_not_a_refusal(
        names, exported):
    """The two ways an attach fails must not look alike.

    "The portal refused this initiator" is a configuration problem on the
    appliance; "nothing answered" is a network or address problem, and
    #64 is open about what the second one does to an attach. A probe that
    collapsed them into one exception would make that issue harder to
    settle, not easier.
    """
    address, port = exported['portals'][0]

    with pytest.raises(OSError) as caught:
        with iscsi_probe.login(address, names.iqn, exported['iqn'],
                               port=port + 1, timeout=5):
            pass

    assert not isinstance(caught.value, iscsi_probe.LoginRefused)


def test_the_login_stops_working_once_the_export_is_removed(
        driver, names, exported):
    """`remove_export` disconnects, and not only on the appliance's word.

    Every existing teardown assertion reads the appliance's own lists back
    -- which is what a driver bug that deleted the wrong thing would also
    satisfy. This asserts the property the operator cares about: after
    removal the disk is no longer reachable.
    """
    address, port = exported['portals'][0]

    driver.remove_export(None, exported['volume'])

    with pytest.raises(iscsi_probe.LoginRefused):
        with iscsi_probe.login(address, names.iqn, exported['iqn'],
                               port=port):
            pass


def test_a_discovery_session_needs_no_access_list(names, exported):
    """Separates "the portal is up" from "this initiator may attach".

    A discovery session carries no target name and no access list, so it
    succeeds for an initiator a normal login would refuse. That makes it
    the check that distinguishes a portal-address mistake from an
    initiator-group one -- the two faults #64 and #18 respectively leave
    behind.
    """
    address, port = exported['portals'][0]

    with iscsi_probe.login(address, names.iqn + '-stranger',
                           port=port) as session:
        assert 'MaxRecvDataSegmentLength' in session['keys']
