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

Deployment target: Kolla-Ansible, OpenStack 2025.1, Ubuntu Jammy base. Base
class is `cinder.volume.drivers.san.san.SanISCSIDriver`.

## Layout

```
.github/
  CODEOWNERS               # * @setkeh
  workflows/test.yml       # unit tests (3.10, 3.12) + flake8
  workflows/claude-code-review.yml
truenas_cinder_driver/
  __init__.py      # exports TrueNASAPIClient, the exception hierarchy, __version__
  api_client.py    # TrueNASAPIClient + TrueNASAPIError hierarchy — REST wrapper
tests/
  __init__.py
  unit/
    __init__.py
    test_api_client.py
tools/
  verify_endpoints.py  # live-appliance verification, reads .env
.env.example       # template; .env itself is gitignored
docs/PLANNING.md   # milestones + issue map (predates the current issues, see #28)
tox.ini            # envlist = py310, flake8; also holds [flake8] config
requirements.txt   # cinder (platform, not installed by CI), requests
test-requirements.txt  # pytest, pytest-cov, coverage, flake8, tox, requests
```

`driver.py` does **not** exist on main. A draft lives on the unmerged
`origin/feature/driver-core` branch — see #26 for its disposition.

There is **no** `setup.py` / `pyproject.toml` / `setup.cfg` — the package is
not installable, so tests import `truenas_cinder_driver` only because the repo
root is on `sys.path`. That is why CI and tox invoke `python -m pytest` rather
than bare `pytest`. Issue #23 fixes this properly.

## Commands

```bash
tox                                        # py310 + flake8
tox -e py310                               # unit tests with coverage
tox -e flake8                              # lint
python -m pytest tests/unit                # direct test run
python -m unittest discover -s tests -t .  # discover path, also run in CI
```

Use `python -m pytest`, not bare `pytest` — see the packaging note above.

**Environment caveats on this machine:**
- `python3` is 3.14; `python3 -m venv` fails — `python3-venv` is not installed.
- `pytest`, `flake8`, `tox`, and `cinder` are **not** installed system-wide;
  only `requests` is. Run the suite with
  `python3 -m unittest discover -s tests -t .` as the fallback — it needs no
  third-party packages and is what has been used to verify every change so far.
- Cinder is deliberately not installed by CI. The driver is loaded *by* a Cinder
  deployment that already provides it, and installing it costs minutes for no
  benefit while only `api_client.py` (requests-only) is under test.

## Standing Hazards

Long-lived facts about this codebase that are easy to get wrong. Point-in-time
status does **not** belong here — that is what the issue tracker is for. Run
`gh issue list` for what is open; the milestones `M1 — Minimum viable attach`,
`M2 — Full lifecycle`, and `M3 — Migration ready` carry the ordering.

**Only the zvol, auth and error-mapping paths have been verified against real
hardware** (TrueNAS-25.10.5, #35 and #11). The iSCSI pipeline (#12) and
snapshot/clone (#13) endpoints are still design-doc guesses. Every guess checked so far has had at
least one error in it — `/zfs/zvol` was not a real endpoint, `volmode: GEOM` is
FreeBSD-only, `name__startswith` is not a valid operator, and the EULA endpoint
returns a bare boolean rather than an object. Verify before building on any
un-exercised path.

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

`.env` is gitignored. Write mode creates exactly one throwaway zvol and removes
it in a `finally` block. Read-only mode also asserts the error mapping —
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

**Auth is a Bearer API key, not a password** (#10). `truenas_api_key` is a
service-account key and must be declared `secret=True` in `oslo_config` so it
is redacted from logged config dumps. `verify_ssl` defaults to **True** — do
not flip it back to make a self-signed certificate work; fix the certificate or
set the option explicitly per deployment.

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

> **OUTSTANDING — re-enable required status checks when starting #8.**
> The ruleset has **no `required_status_checks` rule**, so CI is advisory: a
> PR with a red pipeline can still merge. This was deliberate so #29 could land
> while the unit suite was still broken. Once #8 turns the suite green, add the
> rule with contexts `Unit tests (Python 3.10)`, `Unit tests (Python 3.12)`,
> and `Lint (flake8)`, plus `strict_required_status_checks_policy: true`.

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
2. **Tests required** for new functions — add to `tests/unit/test_<module>.py`
   (note: `test_*.py`, not `*_tests.py`). Mock at the `requests.Session`
   boundary; unit tests must never make a real network call. Verify a new test
   actually fails when the code is wrong — the existing suite is a cautionary
   example of tests that pass no signal.
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
