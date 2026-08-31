"""The iSCSI export pipeline end to end (#12, #25).

Every assertion here is scoped to objects this test created. The script
this replaced asserted that the appliance's *global* `/iscsi/*` lists were
empty afterwards, which meant it failed on any appliance that had an export
on it -- including the dev box once a Cinder deployment lived there (#69).
An always-red check trains its reader to skim past it, which is the same
defect as an always-green one approached from the other side.

Two behaviours contradicted the design spec and are asserted rather than
merely exercised, because both would fail silently if they changed: a
reload does not start a stopped service, and deleting either end of a
target-extent link cascades the link.
"""

import pytest

from truenas_cinder_driver import api_client


@pytest.fixture
def service_restored(client):
    """Return the iscsitarget service to the state it was found in."""
    before = client.get_iscsi_service()["state"]
    yield before
    after = client.get_iscsi_service()["state"]
    if after != before:
        if before == "STOPPED":
            client._make_request("POST", "/service/stop",
                                 json={"service": "iscsitarget"})
        else:
            client.start_iscsi_service()


@pytest.fixture
def portals(client, cleanup):
    """Portal ids to export through, reusing what the appliance has.

    Creating a second portal on an address that already has one is
    refused, so an appliance in use cannot be tested by always creating.
    Only portals this fixture created are tidied up.

    Multipath needs a portal per address, and a portal can only bind a
    *statically* configured address -- `listen_ip_choices` omits DHCP ones
    entirely (#45). A single-homed appliance falls back to the wildcard and
    simply does not exercise multipath.
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
def export(client, pool, zvol, names, portals, cleanup):
    """A complete export around the throwaway zvol."""
    disk = client.zvol_disk_path(pool, zvol)

    group_id = client.get_or_create_initiator_group([names.iqn])
    cleanup(f"initiator group {group_id}", client._make_request,
            "DELETE", f"/iscsi/initiator/id/{group_id}")

    extent_id = client.create_extent(disk, names.extent)
    cleanup(f"extent {extent_id}", client.delete_extent, extent_id)

    target_id = client.create_target(names.target, group_id, portals)
    cleanup(f"target {target_id}", client.delete_target, target_id)

    link_id = client.create_target_extent(target_id, extent_id)
    cleanup(f"link {link_id}", client.delete_target_extent, link_id)

    return {"disk": disk, "group": group_id, "extent": extent_id,
            "target": target_id, "link": link_id}


def test_initiator_group_is_reused_not_duplicated(client, names, export):
    # The whole point of get_or_create: TrueNAS applies no uniqueness
    # constraint, so a second call must not make a second group.
    again = client.get_or_create_initiator_group([names.iqn])

    assert again == export["group"]


def test_a_second_extent_on_the_same_zvol_is_refused(client, names, export):
    with pytest.raises(api_client.TrueNASAPIError) as caught:
        client.create_extent(export["disk"], names.extent + "-dup")

    assert not isinstance(caught.value, api_client.TrueNASAPINotFoundError)


def test_the_target_is_bound_to_the_portals_it_was_given(
        client, names, export, portals):
    bound = client.get_target_by_name(names.target)

    # Compared as a set: the appliance returns groups in a different order
    # than they were sent, and nothing may depend on that order (#45).
    got = sorted(g["portal"] for g in bound["groups"])
    assert got == sorted(portals)


def test_multipath_comes_from_one_portal_listening_on_many_addresses(
        client, portals):
    """The shape `_resolve_portal_addresses` is built on (#45).

    Multipath here is **one** portal bound to several addresses, not
    several portals -- the driver resolves a single portal and reads its
    `listen` list. Asserting one-target-per-portal instead would encode the
    model #45 rejected, and would sit permanently skipped on any appliance
    with a single portal, which is most of them.
    """
    listens = {p["id"]: [entry["ip"] for entry in p["listen"]]
               for p in client.get_portals()}

    for pid in portals:
        assert pid in listens
        assert listens[pid], f"portal {pid} listens on nothing"

    multi = [pid for pid, ips in listens.items() if len(ips) > 1]
    if not multi:
        pytest.skip("no portal binds more than one address; multipath "
                    "needs a multi-homed appliance with static IPs (#45)")

    # The addresses a compute node would be handed, one per path.
    for pid in multi:
        assert len(set(listens[pid])) == len(listens[pid]), (
            f"portal {pid} repeats an address: {listens[pid]}")


def test_name_lookups_find_what_was_just_created(client, names, export):
    # The authoritative teardown path (#16). An unrecognised filter field
    # is not rejected -- the appliance answers 200 with an empty list -- so
    # a broken filter would read as "already gone" and orphan every export.
    assert client.get_target_by_name(names.target)["id"] == export["target"]
    assert client.get_extent_by_name(names.extent)["id"] == export["extent"]


@pytest.mark.parametrize("lookup", ["get_target_by_name",
                                    "get_extent_by_name"])
def test_an_unknown_name_returns_none_rather_than_raising(client, lookup):
    # None is the expected pass value here, so a lookup that *raised*
    # must be distinguished from one that returned None -- otherwise a
    # broken lookup scores as a pass.
    assert getattr(client, lookup)("cinder-func-no-such-name") is None


def test_the_target_extent_link_is_found_by_its_ends(client, export):
    found = client.get_target_extent(export["target"], export["extent"])

    assert found["id"] == export["link"]


def test_a_reload_does_not_start_a_stopped_service(client, service_restored):
    if service_restored != "STOPPED":
        pytest.skip("service already running; nothing to prove")

    reloaded = client.reload_iscsi_service()

    assert not reloaded
    assert client.get_iscsi_service()["state"] == "STOPPED"


def test_starting_the_service_reports_it_running(client, service_restored):
    client.start_iscsi_service()

    assert client.get_iscsi_service()["state"] == "RUNNING"


def test_the_full_iqn_is_derivable_from_the_global_config(
        client, names, export):
    # The string a Nova initiator logs in to.
    basename = client.get_iscsi_global_config()["basename"]

    assert f"{basename}:{names.target}" .startswith("iqn.")


def test_deleting_the_target_cascades_the_link_but_keeps_the_extent(
        client, names, export):
    # The spec claimed TrueNAS does not cascade at all. It does, and
    # remove_export relies on it.
    client.delete_target(export["target"])

    links = [link["id"] for link in client.get_target_extents()]
    assert export["link"] not in links, "the link survived its target"

    extents = [e["id"] for e in client.get_extents()]
    assert export["extent"] in extents, (
        "deleting the target destroyed the extent -- check that "
        "delete_extents is not being sent")


def test_teardown_leaves_none_of_our_objects_behind(
        client, names, export, pool, zvol):
    # Scoped to this test's own objects. Asserting the appliance's global
    # lists are empty is what made the old script fail on any appliance in
    # use (#69); it says nothing about whether *we* leaked.
    client.delete_target(export["target"])
    client.delete_extent(export["extent"])

    assert client.get_target_by_name(names.target) is None
    assert client.get_extent_by_name(names.extent) is None
    # The zvol the export pointed at is untouched by removing the export.
    assert client.get_zvol(pool, zvol)["type"] == "VOLUME"
