# AGENTS.md — TRUENAS-CINDER

**TrueNAS Cinder** — an OpenStack Cinder volume driver for TrueNAS Scale.

Repo: `AtlasDigitalEngineering/TrueNas-Cinder` (public, GPL-3.0).
Language: Python. Default branch: `main`.

## What This Project Is

A Cinder volume driver that lets OpenStack use TrueNAS Scale as a storage
backend. iSCSI is the primary protocol (zvol → iSCSI extent → target →
target-extent association); NFS is a stated future enhancement, not started.

The driver talks to TrueNAS over its **REST API v2.0** (`https://<host>:<port>/api/v2.0`).

**This driver is the critical-path dependency for a Proxmox → OpenStack
(Kolla-Ansible) migration.** Production VM disks already exist as zvols on
TrueNAS, created by Proxmox; the `manage_existing` family adopts them in place
with zero data copy. That constraint drives most of the priority ordering.

**What is authoritative, in order:**

1. **The live appliance** for anything about API behaviour — endpoint paths,
   payload shapes, response shapes, filter syntax. Verify with
   `tools/verify_endpoints.py`; see "Verifying against real hardware" below.
2. **The issue tracker** for scope, acceptance criteria, and ordering.
3. **This file** for conventions and hazards.

There was previously a *TrueNAS Cinder Driver — Development Plan* document
described here as authoritative. It is a **design doc**, not a specification:
useful for intent and shape, but it was never verified against hardware, and
trusting it produced three real defects (`volmode: GEOM`, `name__startswith`,
and the EULA response shape — all fixed in #35). Its content has been extracted
into issues #8–#28 and the milestone definitions. Do not treat any document as
outranking observed behaviour.

Deployment target: Kolla-Ansible, OpenStack 2025.1, **Ubuntu Noble 24.04**
base — verified against Kolla's own `stable/2025.1`
(`kolla/common/config.py` pins `'ubuntu': {'tag': '24.04'}`, and its support
matrix lists Ubuntu Noble). Noble ships **python3 3.12**, so that is the
interpreter the driver actually runs on. Re-check both on a Kolla upgrade;
an earlier version of this file said Jammy, which put the driver tests on
3.10 for months (#57). Base class is
`cinder.volume.drivers.san.san.SanISCSIDriver`.

## Layout

```
.github/
  CODEOWNERS               # * @setkeh
  workflows/test.yml       # unit (3.12, 3.10) + driver (3.12) + flake8
  workflows/claude-code-review.yml
truenas_cinder_driver/
  __init__.py      # exports TrueNASAPIClient, the exception hierarchy, __version__
  api_client.py    # TrueNASAPIClient + TrueNASAPIError hierarchy — REST wrapper
  driver.py        # TrueNASISCSIDriver — config, setup validation, stats
tests/
  __init__.py
  unit/            # api_client; runs on `requests` alone, no Cinder
    __init__.py
    test_api_client.py
  driver/          # driver; needs Cinder installed
    __init__.py
    test_driver.py
tools/
  verify_endpoints.py  # live-appliance verification, reads .env
.env.example       # template; .env itself is gitignored
docs/PLANNING.md   # why the project exists, its shape, milestone outcomes
docs/configuration.md  # sample cinder.conf backend section + prerequisites
flake.nix          # dev shell: python312, uv, gh, LD_LIBRARY_PATH
tox.ini            # envlist = py312, driver, flake8; also [flake8] config
requirements.txt   # cinder (platform), requests
test-requirements.txt  # pytest, pytest-cov, coverage, flake8, tox, requests
driver-test-requirements.txt  # the above plus Cinder, for tests/driver
```

`feature/driver-core` carried an earlier draft of `driver.py`. It was
abandoned and deleted — merging it would have reverted the whole API client.
See #26.

There is **no** `setup.py` / `pyproject.toml` / `setup.cfg` — the package is
not installable, so tests import `truenas_cinder_driver` only because the repo
root is on `sys.path`. That is why CI and tox invoke `python -m pytest` rather
than bare `pytest`. Issue #23 fixes this properly.

## Commands

```bash
tox                                            # py312 + driver + flake8
tox -e py312                                   # api_client tests, no Cinder
tox -e driver                                  # driver tests, needs Cinder
tox -e flake8                                  # lint
python -m pytest tests/unit                    # direct api_client run
python -m unittest discover -s tests/unit -t . # discover path, also in CI
```

Use `python -m pytest`, not bare `pytest` — see the packaging note above.

**Use the flake.** `nix develop` gives the whole development environment.
It comes in two halves, and the split is worth understanding:

**Built by Nix, pinned by `flake.lock`, ready with no setup.** Everything the
API-client suite, the linter and the verification tool need is in nixpkgs, so
it is a `python312.withPackages` env — no venv, no PyPI, nothing to install:

```bash
nix develop
python3 -m pytest tests/unit
python3 -m flake8 truenas_cinder_driver tests tools
python3 tools/verify_endpoints.py [--write]
```

That env deliberately does **not** contain Cinder. `import cinder` failing in it
is the same guarantee the dependency-free CI job gives, enforced locally.

**Installed from PyPI into a venv, for `tests/driver` only:**

```bash
uv venv && uv pip install -r driver-test-requirements.txt
.venv/bin/python -m pytest tests/driver
```

Cinder is not in nixpkgs, and neither are `os-brick`, `oslo-versionedobjects`,
`taskflow`, `castellan` or `cursive`. Making this half reproducible too means
either packaging Cinder's whole dependency tree, or adopting `uv2nix` once #23
adds a `pyproject.toml` — the latter is the better path and belongs to that
issue.

Two things the flake handles that are easy to get wrong by hand, both commented
in it:

- **`LD_LIBRARY_PATH` must carry `libstdc++`.** Cinder pulls `greenlet`, `lxml`
  and `cryptography` as manylinux wheels built against an FHS toolchain, so
  without it `import cinder` dies with `libstdc++.so.6: cannot open shared
  object file`. Only the venv half needs this; the Nix env does not.
- **`UV_PYTHON` must not be exported.** It takes precedence over uv's discovery
  of `./.venv`, so `uv pip install` targets the read-only store interpreter and
  fails with "tries to modify the immutable /nix/store". `UV_PYTHON_DOWNLOADS`
  is set to `never` instead, so `uv venv` uses the shell's Python rather than
  fetching one that will not run on NixOS.

The flake is deliberately self-contained — nixpkgs and flake-utils only. This
repo is public, so the dev shell has to resolve for a contributor with no
access to any personal NixOS configuration.

Local and CI both run 3.12, the deployment interpreter. `python310` is no
longer in nixpkgs, which is convenient rather than a compromise: the api_client
suite keeps a 3.10 leg in CI as breadth, but nothing needs 3.10 locally.

- **Cinder is needed for `tests/driver`, and only there.** `tests/unit` runs on
  `requests` alone so the common CI job stays fast; a separate `Driver tests`
  job installs Cinder and tests against the real base classes. Stubbing Cinder
  was considered and rejected — it would test the driver against a fake base
  class, and would not have caught that `SanDriver.check_for_setup_error()`
  demands SSH credentials this driver never uses.

Use `python -m pytest`, not bare `pytest` — see the packaging note above.

**Environment caveats.** Don't assume a usable interpreter is on `PATH` — the
dev machines differ:
- On the **NixOS** workstation there is no system `python3` at all. Everything
  runs through a shell:
  ```bash
  nix-shell -p python3 python3Packages.requests python3Packages.flake8 \
    --run 'python3 -m flake8 truenas_cinder_driver tools tests'
  nix-shell -p python3 python3Packages.requests \
    --run 'python3 -m unittest discover -s tests -t .'
  ```
- Where a system Python does exist, `pytest`, `flake8`, `tox` and `cinder` are
  generally *not* installed, so
  `python3 -m unittest discover -s tests -t .` is the dependable fallback — it
  needs nothing beyond `requests`.
- **Cinder is needed for `tests/driver`, and only there.** `tests/unit` runs on
  `requests` alone so the common CI job stays fast; a separate `Driver tests`
  job installs Cinder and tests against the real base classes. Stubbing Cinder
  was considered and rejected — it would test the driver against a fake base
  class, and would not have caught that `SanDriver.check_for_setup_error()`
  demands SSH credentials this driver never uses.
- **Driver tests on the NixOS workstation** need Cinder in a venv *and*
  `libstdc++` on the library path, or `greenlet` fails to import:
  ```bash
  nix-shell -p python312 --run 'python3 -m venv /tmp/cenv'
  /tmp/cenv/bin/pip install -r driver-test-requirements.txt
  LD_LIBRARY_PATH="$(nix-build '<nixpkgs>' -A stdenv.cc.cc.lib --no-out-link)/lib" \\
    /tmp/cenv/bin/python -m pytest tests/driver
  ```
  Cinder 26.3.0 installs cleanly on Python 3.12 this way — which is also the
  deployment interpreter, so local runs and the driver CI job agree. `python310`
  is not in nixpkgs; CI keeps a 3.10 leg for the api_client suite as breadth.
- **Clear `__pycache__` before trusting a test result after a scripted edit.**
  A same-size change written within the same mtime second leaves a stale `.pyc`
  that Python considers valid, so tests run against the *previous* source. This
  produced a phantom failure during #12's mutation run. `python3 -B` avoids it.

## Standing Hazards

Long-lived facts about this codebase that are easy to get wrong. Point-in-time
status does **not** belong here — that is what the issue tracker is for. Run
`gh issue list` for what is open; the milestones `M1 — Minimum viable attach`,
`M2 — Full lifecycle`, and `M3 — Migration ready` carry the ordering.

**The zvol, auth, error-mapping, iSCSI-pipeline and snapshot paths have been
verified against real hardware** (TrueNAS-25.10.5, #35, #11, #12 and #42). The
clone, rollback and promote endpoints (#13, #21) have not.
Every design-doc guess checked so far has had at least one error in it —
`/zfs/zvol` was not a real endpoint, `volmode: GEOM` is FreeBSD-only,
`name__startswith` is not a valid operator, the EULA endpoint returns a bare
boolean rather than an object, and the iSCSI extent payload was wrong in two
fields at once. Verify before building on any un-exercised path.

The appliance serves its own OpenAPI document at `/api/v2.0/openapi.json`
(610 paths). It is authoritative for field names, types, enums and defaults,
and is far cheaper than guessing and then probing. Read it first; probe to
confirm behaviour the schema cannot express (cascades, defaults that lie,
silent no-ops).

Two traps found while verifying, both of which mislead rather than fail loudly:
an unrecognised key in a `/pool/dataset` create breaks discrimination of the
`VOLUME` schema variant, so the 422 blames `type` instead of the real culprit;
and the JSON `filters=[[...]]` query form returns `200` with an empty list
rather than an error, so a wrong query reads as "nothing exists".

**Never point tests or exploration at the production TrueNAS.** It holds every
production VM disk as a zvol, and those are the migration's only copy. Use the
dev appliance and a scratch pool; `tools/verify_endpoints.py` refuses to run
without both configured, but that check is not a substitute for looking.

## Verifying against real hardware

```bash
cp .env.example .env     # fill in URL, API key, scratch pool
python3 tools/verify_endpoints.py            # read-only
python3 tools/verify_endpoints.py --write    # + throwaway zvol lifecycle
```

`.env` is gitignored. Write mode creates exactly one throwaway zvol, takes a
snapshot of it, exports it through the full iSCSI pipeline (portal → initiator
group → extent → target → target-extent link, plus a service start), and
removes every resource in a `finally` block — including returning `iscsitarget`
to the state it was found in. It asserts the pipeline and snapshot traps listed
above rather than merely exercising them, so a behaviour change on a future
TrueNAS release fails loudly instead of silently invalidating the client. Read-only mode also asserts the
error mapping —
`expect_raises` checks that each not-found form still produces
`TrueNASAPINotFoundError` and that an errno-22 validation error still does
not. That mapping rests on undocumented status codes, so it is the part most
likely to drift on a TrueNAS upgrade.

**This is a manual, local step — CI does not run it.** No workflow invokes
`tools/verify_endpoints.py`, and nothing consumes the `DEV_TRUENAS_API_KEY`
repo secret, which exists only in anticipation of the functional suite in #25.
Wiring it into CI is not simply a matter of adding a job: GitHub-hosted runners
have no route to a private-LAN appliance, so it needs a self-hosted runner or a
reachable test target. Re-verification happens when someone runs the script.

Extend this script when adding client methods — the point is that findings can
be re-checked and re-run against a new TrueNAS release, not taken on trust.

**Every client failure is a `TrueNASAPIError` subclass** (#11) — including
network ones, so a caller never sees a raw `requests` exception and `#14` can
translate to `VolumeBackendAPIException` with one `except`. Requests carry a
default `(10s, 60s)` timeout; 429 and 503 are retried with backoff. **Timeouts
are deliberately not retried** — a read timeout does not mean the appliance
stopped working on the request, so replaying a create or delete on top of one
is worse than failing. Making retry idempotency-aware is #12's problem.

**"Object not found" has two forms, and the obvious one is the wrong one.**
Verified on TrueNAS-25.10.5 (#11):

| Operation                        | Status | Body            |
| -------------------------------- | ------ | --------------- |
| `GET /pool/dataset/id/<missing>`  | 404    | `{"message": ""}` |
| `DELETE /pool/dataset/id/<missing>` | 422  | `errno: 2`      |
| `PUT /pool/dataset/id/<missing>`  | 422    | `errno: 2`      |
| `DELETE /iscsi/extent/id/<missing>` | 422  | `errno: 2`      |

`DELETE` — the one operation idempotent deletes depend on — is in the 422
group, so a 404-only mapping compiles, passes review, and never fires where it
matters. Match on **`errno`, never the message text**: creating into a
nonexistent pool returns errno `22` with the message `zpool (X) does not
exist.`, and string matching would report that failed create as a successful
delete. `_is_enoent` requires *every* reported error to be ENOENT, because a
false "already gone" loses a volume while a false "still there" only fails a
no-op.

**A mistyped endpoint also returns 404**, so a caller swallowing
`TrueNASAPINotFoundError` for idempotency will read a wrong path as a
successful delete. Run `tools/verify_endpoints.py` against real hardware
before trusting any new path.

**Snapshots live under `/pool/snapshot`, and their ids must be percent-encoded.**
A snapshot id is `{pool}/{dataset}@{snapshot}`. Interpolated raw into a URL the
`/` characters become extra path segments and the appliance answers **404**, so
`delete_snapshot` percent-encodes via `_snapshot_path` (#42).

That combination is the sharpest example of the mistyped-endpoint hazard in this
codebase, and worth understanding rather than just obeying. Before #42 these
methods had **two** independent bugs — the legacy `/zfs/snapshot` base path
(404, in *plain text*) and the unencoded id (also 404) — and both produced a
`TrueNASAPINotFoundError`, which callers swallow to make deletes idempotent. Two
stacked bugs therefore cancelled into a method that reported success on every
call while deleting nothing, forever. Neither a green unit suite nor a code
review catches that; only exercising the path against hardware does.
`tools/verify_endpoints.py` now asserts both wrong forms are *still* wrong, so
reintroducing either fails loudly.

Three more verified snapshot behaviours:

- **A zvol with a live snapshot refuses a non-recursive delete** — "volume has
  children ... use '-r'", a plain error rather than ENOENT. `delete_zvol`
  defaults to `recursive=False`, which is correct: silently destroying a
  volume's snapshots is not something `delete_volume` should do by accident.
- **Creating a snapshot that already exists is errno 17 (EEXIST), not 2.** It
  surfaces as a plain `TrueNASAPIError` and must never be read as "already
  gone".
- **The unfiltered snapshot list includes the appliance's boot-pool snapshots**
  — eight on a clean 25.10.5 install. Pass `dataset=` unless you truly want
  everything.

`/pool/snapshot/rename` exists and works, so #19's fallback is unnecessary —
but never pass `force: true`, which renames a dataset that is in use and can
disrupt a live iSCSI target.

**The iSCSI pipeline has four traps, all verified in #12.**

1. **Portals are not pre-existing, and need a static address.** The design doc
   called them read-only from the driver's perspective; a clean appliance has
   *zero*. `get_portals()` returns `[]` and there is no portal ID to create a
   target with. Whether the driver creates one or demands one is #14/#17's call.

   `GET /iscsi/portal/listen_ip_choices` lists **only statically configured
   addresses** — its own schema says so, and a DHCP interface is omitted even
   though it holds a real address (#45). A portal bound to `0.0.0.0` reports
   `listen[].ip` as `0.0.0.0`, which no compute node can connect to. So
   "a portal exists" is not sufficient; it must bind a usable address.
2. **A reload does not start a stopped service.** On a fresh appliance
   `iscsitarget` is `STOPPED` with `enable: false`. `POST /service/reload`
   returns `false` and changes nothing, so every target and extent written is
   inert and *nothing reports an error*. Check `get_iscsi_service()` before
   trusting a reload. `start_iscsi_service()` does not survive a reboot either;
   that needs `enable: true` via `POST /service/update`.
3. **TrueNAS does cascade**, contrary to the design doc. Deleting either a
   target or an extent removes the target-extent link between them; the other
   end survives. Explicit ordered teardown still works, because the redundant
   delete returns 422/errno 2 and lands in `TrueNASAPINotFoundError`. Do not
   "fix" a rollback path to depend on the absence of cascading.
4. **Duplicate initiator groups are allowed.** Posting identical `initiators`
   twice yields two groups — TrueNAS enforces no uniqueness — so
   `get_or_create_initiator_group` dedupes client-side, on set equality. An
   *empty* initiators list means "allow every initiator", so the client refuses
   one rather than silently exporting a volume to the whole network.

**Server-side filters work on the iSCSI collections, but a wrong field fails
silently.** `GET /iscsi/target?name=<name>` and `GET /iscsi/extent?name=<name>`
filter on the appliance — verified in #16 by creating *two* exports and
confirming each name returned exactly its own row, which an ignored filter
could not fake. Use `params={"name": ...}`, as `list_zvols` does; never the
`filters=[[...]]` JSON form.

The asymmetry to remember: an invalid **operator** fails loudly
(`name__startswith` → 422 "Invalid operation: startswith") but an unrecognised
**field** does not — it returns `200 []`, confirmed against a collection holding
seven rows. **An empty result is therefore not evidence that a name is absent**;
it may mean the filter was never applied. `_get_one_by_name` raises on more than
one row for the mirror image of the same reason: a filter that was ignored
returns the whole collection, and taking `[0]` would hand back a different
volume's export to be deleted.

Name lookup is the **authoritative** way to find a volume's target and extent at
teardown (#16). Nothing cached is trusted for deletion: TrueNAS ids are small
integers, so a stale id could address another volume's export, and guarding
against that costs the same one request as looking it up by name. `provider_id`
records `target:extent` for diagnostics, orphan reconciliation and #20 — never
to skip a lookup.

**Multipath is one target bound to several portals, and the appliance
reorders the groups.** A TrueNAS target carries one group per portal, all
sharing an initiator group — which IQNs may connect is a property of the volume,
not of the path they arrive by. `create_target()` takes one portal id or a list
of them; verified against hardware in #45.

Cinder needs no help presenting this. `ISCSIDriver._get_iscsi_properties()`
parses `provider_location` as `"<ip1>:<port>;<ip2>:<port>,<tag> <IQN> <lun>"` and,
when there is more than one portal, populates `target_portals` / `target_iqns` /
`target_luns` with one IQN and one LUN repeated per portal, keeping the singular
keys for backward compatibility. Single-portal is the same format without
semicolons, so there is one code path rather than two, and the `,<tag>` field is
discarded by the parser — it is cosmetic, and cannot represent the per-portal
tags TrueNAS actually assigns.

**Never derive portal ordering from the appliance.** Posting groups for portals
`[11, 12]` returns them as `[12, 11]` (#45). The first portal in
`provider_location` becomes the singular `target_portal` a non-multipath
connector uses, so reading the order back would let it flip between attaches.
Build it from the configured address list. `create_target()` returns only the new
id, deliberately, so there is no reordered `groups` list to be tempted by.

**Never send the destructive delete options.** `DELETE /iscsi/target` accepts
`delete_extents`, `DELETE /iscsi/extent` accepts `remove`, and both accept
`force`. All default to false and the client sends none of them; `delete_extents`
in particular would turn detaching a volume into destroying its export.

**Target and extent names are constrained**: lowercase alphanumerics plus `.`,
`-` and `:` only. Cinder's default `volume-<uuid>` passes, but a deployment that
puts an underscore or a capital in `volume_name_template` fails at first attach.
`validate_target_name()` is the appliance's own pre-flight check for this.

A zvol backs **at most one extent** — a second attempt fails with "Disk
currently in use by extent \<name\>" (errno 22, not ENOENT). That 1:1
constraint is enforced appliance-side, which is what makes name-based
re-derivation viable for #16.

**Two things in `driver.py` look removable and are not.**

`check_for_setup_error()` **must not call `super()`**. `SanDriver`'s version
raises `InvalidInput` unless `san_ip` and one of `san_password` /
`san_private_key` are set, because it drives arrays over SSH. This driver only
speaks REST with a Bearer key, so those options are unused and demanding them
would block startup on credentials nothing reads. A test asserts the driver
starts with all of them empty, so "restoring" the call fails CI.

`_update_volume_stats()` **must stay overridden**. The inherited
`ISCSIDriver` version reports `total_capacity_gb=0`, `free_capacity_gb=0` and
`reserved_percentage=100`, which the scheduler's capacity filter rejects — the
backend would silently accept no volumes at all, and nothing would say why.
Capacity comes from `GET /pool`, which reports `size` and `free` in bytes.

**An export is inert until the service reloads.** `create_export` reloads
after building the pipeline, and rolls the whole thing back if that reload
fails — configuration the appliance accepted but never activated is not an
export, and leaving it behind would orphan a target and extent that no
initiator can see. `remove_export` reloads too, but a failure there is only
logged: the resources are already gone, and failing would strand the volume.

**The driver never initialises appliance state** (#14). It does not create a
portal and does not start the iSCSI service, because both are appliance-wide
and shared by every `cinder-volume` worker. `check_for_setup_error` validates
instead, and every failure names the offending value, the config option and the
remedy. `docs/configuration.md` carries the operator-facing prerequisites.

**Auth is a Bearer API key, not a password** (#10). `truenas_api_key` is a
service-account key and must be declared `secret=True` in `oslo_config` so it
is redacted from logged config dumps. `verify_ssl` defaults to **True** — do
not flip it back to make a self-signed certificate work; fix the certificate or
set the option explicitly per deployment.

A `base_url` containing inline credentials (`https://user:pass@host`) is
**rejected at construction** (#11), for two verified reasons: requests turns
the userinfo into a Basic header that *overwrites* the Bearer key, silently
discarding the API key; and it keeps the userinfo in `response.url`, which
`raise_for_status()` bakes into the `HTTPError` chained as `__cause__` — so
`LOG.exception` prints the password no matter how carefully this module words
its own messages. **Wording your own exception messages carefully is not a
credential guarantee** when you chain an upstream exception; check what the
whole formatted chain contains, not just `str(err)`.

**The `feature/driver-core` draft does not import.** `driver.py` on that branch
crashes in `__init__` (`.lower()` on a bool), calls client methods that do not
exist, treats dict responses as objects, reads config from `os.environ` instead
of `self.configuration`, and returns the TrueNAS admin password as the CHAP
secret. Treat it as a design sketch, not a merge candidate — see #26, #14, #15.

**Issues #1–#5 are umbrella trackers**, not work items. Each carries an audit
comment mapping it to the development spec. The actual work lives in the
specific issues they reference. They close via PR like anything else.

**Tests must carry signal, not just pass.** The original suite passed CI review
twice while silently attempting live DNS. Before claiming a test works, break
the production code deliberately and confirm the test fails. Mutation runs on
#30 and #34 are the reference for what that looks like.

## GitOps Workflow

Every unit of work — whether it starts as a `TaskCreate` task or is requested
directly — follows this flow:

0. **Read before you write.** Before resuming or starting work on an issue,
   check its comment thread (and its PR's, if one already exists) for
   context — decisions made, blockers found, direction changes. Don't
   re-derive context that's already sitting there in comments.
1. **Issue first.** Before starting work, create a GitHub issue
   (`gh issue create`) with:
   - A concrete, specific title (not "fix bug" — name the actual thing).
   - A description covering context/why, what needs to change, and
     acceptance criteria.
   - Labels: one type (`bug`/`enhancement`/`documentation`), plus a scope label
     where one fits. **Existing labels in this repo are:** `bug`,
     `documentation`, `duplicate`, `enhancement`, `good first issue`,
     `help wanted`, `invalid`, `question`, `wontfix`, `core`, `testing`,
     `security`, `migration`, `deployment`. There is no `chore` label.
     Create a new label with `gh label create` rather than inventing one
     inline; don't use `area:*` labels — they don't exist here.
   - Milestone: `M1 — Minimum viable attach`, `M2 — Full lifecycle`, or
     `M3 — Migration ready` (see the Open Issues section).
   - Assignee: setkeh, unless the work is fully self-contained and doesn't
     need his action to complete.
2. **Branch per issue.** `<type>/<issue-number>-<short-slug>`, e.g.
   `feat/3-driver-core-config`, `bug/8-api-client-test-mocks`. Branch from
   main.
3. **Comment as you go.** Post progress as comments on the issue while work
   is ongoing (`gh issue comment`) — findings, blockers, decisions made
   mid-task. This is how work-in-progress stays visible, not just the final
   PR diff.
4. **Commits are one-line summaries, no bodies.** Detail lives in the issue
   (while work is ongoing) or the PR description (once one exists) — not in
   the commit message. Never put `Closes #<issue-number>` in a commit
   message.
5. **PR to close it out.** Open a PR (`gh pr create`) from the branch into
   main. The PR description carries the detail a commit body would
   otherwise have, and includes `Closes #<issue-number>` — issues are
   **only** ever closed via the PR that resolves them (on merge), never
   manually and never by a commit. setkeh is both assignee and requested
   reviewer — his approval is the required gate, no one else's.
6. **Claude review.** `.github/workflows/claude-code-review.yml` triggers on
   every PR open/push and posts review comments against the conventions in this
   file. Set up as of 2026-07-26 via `/install-github-app`, which added the
   Claude GitHub App and the repo secret `CLAUDE_CODE_OAUTH_TOKEN` (not a raw
   `ANTHROPIC_API_KEY`).

   It **cannot** approve, merge, or push commits — but it is *not* purely
   advisory: because the ruleset sets `required_review_thread_resolution: true`,
   **any inline comment it leaves blocks the merge** until a human resolves the
   thread. The review prompt therefore reserves inline comments for genuinely
   blocking findings and routes nits into a `### Non-blocking observations`
   section of the summary comment instead (issue #32). Do not undo that split
   without understanding the ruleset interaction — it previously caused three
   consecutive fix-push-renit rounds on #30 with zero real defects found.

   File non-blocking observations as `nit`-labelled issues rather than fixing
   them in the PR they were raised against.

   Fork PRs are skipped: they receive no secrets, so the action would no-op and
   still report a green check. Never "fix" that with `pull_request_target` —
   it runs untrusted code with write permissions.
7. **Merge after approval**, which closes the linked issue automatically.

**Branch protection is enabled** on `main` via the `Gitops` ruleset (id
19733864, `enforcement: active`, targets `~DEFAULT_BRANCH`): blocks deletion
and non-fast-forward, requires signed commits, requires a PR with stale-review
dismissal and thread resolution, and requires code owner review
(`.github/CODEOWNERS` → `* @setkeh`).

Required status checks **are** enforced: contexts `Unit tests (Python 3.12)`,
`Unit tests (Python 3.10)`, `Driver tests` and `Lint (flake8)`, with
`strict_required_status_checks_policy: true`. A red pipeline blocks the merge,
and a branch behind `main` must be updated before it can merge.


**Signed commits are mandatory** and the signing key lives on a hardware token
(OpenPGP smartcard). Agents **cannot** commit — gpg needs a PIN via pinentry on
a TTY. Stage and verify the work, then ask setkeh to run the commit himself.
Never disable signing to work around this.

**Watch for solo-maintainer deadlocks.** `require_last_push_approval` was
enabled initially and made every PR permanently unmergeable — setkeh authors
every PR and GitHub forbids self-approval, with `bypass_actors: []`. It is now
`false`. `require_code_owner_review: true` carries the same risk once
`.github/CODEOWNERS` is on `main`; if a PR reports `mergeable_state=blocked`
with no failing required check, that is the cause. The fix is to add setkeh as
a bypass actor with `bypass_mode: "pull_request"` — this keeps the rule
meaningful for future contributors while still forcing the PR flow.

**No AI attribution, anywhere** — not in commit messages, not in PR/issue
titles or descriptions, not in issue/PR comments. No `Co-Authored-By:
Claude`, no session links, nothing. This is a standing rule, not
per-request.

## Rules for Agents

1. **Match existing code style**: PEP 8, 4-space indent, module-level
   docstrings, Google-style docstrings with `Args:`/`Returns:` on every public
   method, `typing` annotations on signatures. Keep lines within flake8's
   default 79 columns unless a config says otherwise.
2. **Tests required** for new functions (note: `test_*.py`, not `*_tests.py`).
   Which suite, and what to mock, depends on what is under test:
   - `api_client.py` → `tests/unit/test_api_client.py`, mocking at the
     **`requests.Session`** boundary. This suite must keep running without
     Cinder installed.
   - `driver.py` → `tests/driver/test_driver.py`, mocking at the
     **API-client** boundary. The client's own behaviour is already covered
     exhaustively and verified against hardware, so re-mocking HTTP there
     would test the wrong thing.

   No test may make a real network call. Verify a new test actually fails when
   the code is wrong — the original suite is a cautionary example of tests that
   passed no signal.
3. **Follow OpenStack Cinder conventions** in `driver.py`: subclass
   `cinder.volume.drivers.san.san.SanISCSIDriver`, declare config via
   `oslo_config` opts read through `self.configuration`, and raise
   `cinder.exception.*` rather than bare `Exception`.
4. **Never log or return credentials.** No passwords in connection-info dicts,
   exception messages, or debug output.
5. **Update this file in the same PR that invalidates it.** If a change makes
   any statement here wrong — a hazard resolved, a command changed, a file
   added or removed, a convention revised — fix it in that PR, not later. This
   file is the first thing an agent reads and it is trusted on sight, so a
   stale line actively misleads rather than merely aging.

   This is not hypothetical: within one day of work, this file simultaneously
   claimed the test suite was entirely broken, that no `.github/` directory
   existed, and that zvols used `/zfs/zvol` — all three fixed by merged PRs,
   none corrected at the time. Issue #28 exists because of it.

   The structure is built to decay slowly — keep it that way. Point-in-time
   status (what is merged, what is open, who is working on what) belongs in the
   issue tracker, never here. "Standing Hazards" holds only durable facts, each
   pointing at the issue that owns it. Resist re-introducing a dated
   "Current State" section; it has to be rewritten on every merge and will not
   be.
6. **Follow the GitOps Workflow above** for every task — issue, branch, PR,
   review, merge. See that section for the exact conventions.
