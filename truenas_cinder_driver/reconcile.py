"""Find TrueNAS resources that no longer correspond to a Cinder volume.

Anything that fails between creating an export and persisting the model
update can leave a target or extent behind. `remove_export` is the
failure-path cleanup and is idempotent, but it cannot run if
`cinder-volume` died. Across a migration of hundreds of disks those
accumulate until an appliance limit is hit, and the symptom is a *create*
failing for an apparently unrelated reason.

**Reporting only.** Nothing here deletes. Acting automatically on a
reconciliation that might be wrong -- a driver pointed at the wrong pool,
a volume list fetched for the wrong host -- is how a bug becomes data
loss. Callers that want to remediate are handed a list and must decide.

The distinction this module exists to preserve is between a **leak** and
an **adoption candidate**. From the appliance's side they look identical:
both are objects with no Cinder volume behind them. They are told apart by
name. A zvol called `volume-<uuid>` was created by this driver and having
no Cinder record means something went wrong; a zvol called
`vm-100-disk-0` was made by somebody else and is a perfectly healthy
candidate for `manage_existing`. Conflating them would invite an operator
to delete the disks they are trying to migrate.

No Cinder imports: the logic is exercisable without the driver's
dependencies, which is why it lives here rather than on the driver.
"""

import collections
from typing import Any, Dict, List, Optional, Set


# Keys in the report, in the order a human wants to read them: things that
# are certainly wrong first, things that merely warrant a look last.
ORPHAN_CLASSES = (
    "leaked_targets",
    "leaked_extents",
    "unlinked_extents",
    "dangling_links",
    "leaked_zvols",
    "duplicate_initiator_groups",
    "unused_initiator_groups",
    "adoptable_zvols",
)

# Classes that mean something is broken, as opposed to merely notable.
# `adoptable_zvols` is deliberately absent: those are healthy.
LEAK_CLASSES = (
    "leaked_targets",
    "leaked_extents",
    "unlinked_extents",
    "dangling_links",
    "leaked_zvols",
    "duplicate_initiator_groups",
)


def find_orphans(
    client,
    pool: str,
    volume_names: Set[str],
    connector_iqns: Optional[Set[str]] = None,
    volume_prefix: str = "volume-",
) -> Dict[str, List[Any]]:
    """Compare the appliance against the volumes Cinder believes it has.

    Args:
        client: A configured TrueNASAPIClient
        pool: The pool the driver is configured for. Only zvols here are
            considered; the appliance may serve unrelated pools.
        volume_names: Names of the volumes Cinder currently has on this
            backend -- ``volume-<uuid>``, as `volume.name` renders them.
        connector_iqns: IQNs of hosts still expected to attach. Used only
            to flag initiator groups nothing will use again; omit it to
            skip that check rather than guess.
        volume_prefix: The literal prefix `volume_name_template` produces.
            Objects without it were not created by this driver, and this
            module will not call them leaked.

    Returns:
        A mapping from each name in :data:`ORPHAN_CLASSES` to the rows
        found. Empty lists mean nothing of that kind is outstanding.
    """
    report = {name: [] for name in ORPHAN_CLASSES}

    targets = client.get_targets()
    extents = client.get_extents()
    links = client.get_target_extents()
    zvols = client.list_zvols(pool)
    groups = client.get_initiator_groups()

    target_ids = {target["id"] for target in targets}
    extent_ids = {extent["id"] for extent in extents}
    linked_extents = {link["extent"] for link in links}

    def ours(name):
        return bool(name) and name.startswith(volume_prefix)

    for target in targets:
        if ours(target.get("name")) and target["name"] not in volume_names:
            report["leaked_targets"].append(target)

    for extent in extents:
        name = extent.get("name")
        if not ours(name):
            # Somebody else's extent. It may well be unlinked and idle,
            # and that is their business -- a hand-provisioned disk part
            # way through being wired up looks exactly like this. Naming
            # it here would put it in front of --delete-exports, which
            # would then remove configuration this driver never made.
            continue
        if name not in volume_names:
            report["leaked_extents"].append(extent)
        elif extent["id"] not in linked_extents:
            # Ours, still has a Cinder volume, but no target: a half-built
            # export. Deleting a target cascades the link and leaves the
            # extent behind (verified in #12), so this is what a failed
            # teardown looks like. It holds the zvol open and blocks a
            # later create on the same name.
            report["unlinked_extents"].append(extent)

    for link in links:
        if (link["target"] not in target_ids
                or link["extent"] not in extent_ids):
            report["dangling_links"].append(link)

    for zvol in zvols:
        name = zvol["name"].split("/", 1)[-1] if "/" in zvol["name"] else ""
        if not ours(name):
            # Somebody else made this. It is a `manage_existing` candidate,
            # not a leak, and must never be presented as one.
            report["adoptable_zvols"].append(zvol)
        elif name not in volume_names:
            report["leaked_zvols"].append(zvol)

    by_iqns = collections.defaultdict(list)
    for group in groups:
        by_iqns[frozenset(group.get("initiators") or ())].append(group)
    for iqns, members in by_iqns.items():
        if len(members) > 1:
            # `get_or_create_initiator_group` deduplicates on this side
            # because TrueNAS enforces no uniqueness, and it races under
            # concurrent attach (#18). Duplicates are that race, observed.
            report["duplicate_initiator_groups"].append(list(members))
        if connector_iqns is not None and iqns and not (iqns & connector_iqns):
            report["unused_initiator_groups"] += members

    return report


def has_leaks(report: Dict[str, List[Any]]) -> bool:
    """Say whether a report contains anything actually wrong.

    Adoption candidates are excluded: a pool full of disks waiting to be
    migrated is a healthy state, and a check that called it a failure
    would be ignored within a week.

    Args:
        report: A report from :func:`find_orphans`

    Returns:
        True if any leak class is non-empty
    """
    return any(report[name] for name in LEAK_CLASSES)


def describe(report: Dict[str, List[Any]], pool: str) -> str:
    """Render a report for a human.

    Args:
        report: A report from :func:`find_orphans`
        pool: The pool it was taken against, for the header

    Returns:
        A multi-line summary
    """
    lines = ["Reconciliation against pool %s" % pool, ""]

    renderers = {
        "leaked_targets": lambda row: "target %s %r" % (row["id"],
                                                        row["name"]),
        "leaked_extents": lambda row: "extent %s %r -> %s" % (
            row["id"], row["name"], row.get("disk")),
        "unlinked_extents": lambda row: "extent %s %r -> %s" % (
            row["id"], row["name"], row.get("disk")),
        "dangling_links": lambda row: (
            "link %s (target %s, extent %s)" % (row["id"], row["target"],
                                                row["extent"])),
        "leaked_zvols": lambda row: "zvol %s" % row["name"],
        "adoptable_zvols": lambda row: "zvol %s" % row["name"],
        "duplicate_initiator_groups": lambda rows: (
            "groups %s all hold %s" % (
                ", ".join(str(row["id"]) for row in rows),
                sorted(rows[0].get("initiators") or ()))),
        "unused_initiator_groups": lambda row: "group %s %s" % (
            row["id"], row.get("initiators")),
    }
    headings = {
        "leaked_targets": "Targets this driver created with no Cinder volume",
        "leaked_extents": "Extents this driver created with no Cinder volume",
        "unlinked_extents": "Extents with no target (they still hold a zvol)",
        "dangling_links": "Target-extent links pointing at nothing",
        "leaked_zvols": "Zvols this driver created with no Cinder volume",
        "duplicate_initiator_groups": (
            "Duplicate initiator groups (the #18 race, observed)"),
        "unused_initiator_groups": "Initiator groups no known host will use",
        "adoptable_zvols": (
            "Zvols nothing else made -- NOT leaks, these are "
            "manage_existing candidates"),
    }

    for name in ORPHAN_CLASSES:
        rows = report[name]
        if not rows:
            continue
        lines.append("%s (%d)" % (headings[name], len(rows)))
        for row in rows:
            lines.append("    %s" % renderers[name](row))
        lines.append("")

    if not has_leaks(report):
        lines.append("No leaks found.")
        if report["adoptable_zvols"]:
            lines.append(
                "The zvols listed above are adoption candidates, not "
                "problems.")
    return "\n".join(lines)
