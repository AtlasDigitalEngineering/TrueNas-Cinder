# Project Planning and Roadmap

Why this project exists, how it is shaped, and what each milestone means.

**Status lives in the issue tracker, not here.** What is merged, in progress or
blocked changes weekly; this document deliberately holds only the parts that do
not. Run `gh issue list` and see the milestones for current state.

## Why

Production VM disks already exist as zvols on TrueNAS Scale, created by Proxmox.
This driver is the critical-path dependency for migrating that estate to
OpenStack under Kolla-Ansible, and the `manage_existing` family is what adopts
those zvols in place with zero data copy.

That constraint drives most of the priority ordering: features that let existing
data be adopted matter more than features that create new volumes elegantly.

## What is authoritative

In order:

1. **The live appliance** for anything about API behaviour — endpoint paths,
   payload shapes, response shapes, filter syntax. The functional suite
   exists so findings can be re-checked rather than taken on trust.
2. **The issue tracker** for scope, acceptance criteria and ordering.
3. **`AGENTS.md`** for conventions and standing hazards.

A *TrueNAS Cinder Driver — Development Plan & Implementation Spec* exists and an
earlier version of this file described it as authoritative. It is not. It is a
design document: useful for intent and shape, never verified against hardware,
and trusting it produced real defects — `/zfs/zvol` was not an endpoint,
`volmode: GEOM` is FreeBSD-only, `name__startswith` is not a valid operator, and
the EULA endpoint returns a bare boolean. **No document outranks observed
behaviour.** Issue #5 covers bringing what remains useful of it into `docs/`.

## Shape

Two modules, deliberately separable:

- **`truenas_cinder_driver/api_client.py`** — a REST wrapper over the TrueNAS
  API. Knows nothing about Cinder, depends only on `requests`, and every failure
  it produces is a `TrueNASAPIError` subclass. Testable, and tested, without
  OpenStack installed.
- **`truenas_cinder_driver/driver.py`** — the Cinder volume driver, subclassing
  `cinder.volume.drivers.san.san.SanISCSIDriver`. Translates Cinder's lifecycle
  into client calls and `TrueNASAPIError` into `cinder.exception.*`.

The split is why the test suites are split (`tests/unit` needs no Cinder,
`tests/driver` does), and why the API client could be completed and verified
against real hardware before the driver existed.

Storage is presented over **iSCSI**: a zvol becomes an extent, an extent is
joined to a target, and the target is reachable through one or more portals. NFS
is a stated future enhancement, not started.

## Milestones

Defined by outcome rather than by issue list, so they stay meaningful as issues
come and go.

**M1 — Minimum viable attach.** Volumes can be created, deleted, attached and
detached, and the scheduler will place them. Proven by a Nova instance booting
from a TrueNAS-backed Cinder volume — which requires a real Cinder deployment,
not just the driver behaving correctly against the appliance.

**M2 — Full lifecycle.** Snapshots, clones and extend, with rollback paths
covered. The point at which the driver is useful for day-to-day operation rather
than demonstration.

**M3 — Migration ready.** The `manage_existing` family complete and validated
against a real Proxmox-created zvol, a Kolla image built and deployed, and the
identified gaps closed. The point at which the Proxmox estate can actually move.

## Principles

- **Verify against hardware.** Every endpoint this driver depends on has been
  exercised against a real appliance. Every design-doc assumption checked so far
  has been wrong at least once.
- **Fail loudly.** The driver validates its preconditions and refuses to start
  with an actionable message rather than proceeding into a state where nothing
  works and nothing explains why. It never mutates appliance-wide state to make
  itself work.
- **Tests must carry signal.** A test that cannot fail is worse than no test,
  because it reads as coverage. New tests are checked by breaking the code they
  cover.
- **Production is not a test environment.** The production appliance holds the
  only copy of every VM disk in the migration.
