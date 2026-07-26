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

**Authoritative implementation reference:** the *TrueNAS Cinder Driver —
Development Plan & Implementation Spec* document. It carries the exact endpoint
paths, payload shapes, method signatures, gaps G1–G7, and milestone definitions.
Where this file and the spec disagree, the spec wins — and fix this file. Note
the spec is currently an external document, not in `docs/`; issue #5 covers
bringing it in.

Deployment target: Kolla-Ansible, OpenStack 2025.1, Ubuntu Jammy base. Base
class per the spec is `cinder.volume.drivers.san.san.SanISCSIDriver`.

## Layout

```
.github/
  CODEOWNERS               # * @setkeh
  workflows/test.yml       # unit tests (3.10, 3.12) + flake8
  workflows/claude-code-review.yml
truenas_cinder_driver/
  __init__.py      # exports TrueNASClient, __version__ ("0.1.0")
  api_client.py    # TrueNASClient — thin REST wrapper over the TrueNAS v2.0 API
tests/
  __init__.py
  unit/
    __init__.py
    test_api_client.py
docs/PLANNING.md   # milestones + issue map (predates the spec, see #28)
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

**Nothing has ever been verified against a real TrueNAS appliance.** Every
endpoint path, payload shape, and response shape in `api_client.py` comes from
the development spec, not from observed behaviour. `/zfs/zvol` shipped in PR #7
and survived until #9 despite not being a valid endpoint at all. Treat any
un-exercised path as unproven. Tracked in #35.

**Never point tests or exploration at the production TrueNAS.** It holds every
production VM disk as a zvol, and those are the migration's only copy. A
dedicated test appliance is being provisioned; verify the target host and use a
scratch pool before issuing anything that is not a read.

**`api_client.py` is still incomplete** — no token auth (#10), no typed
exceptions, retry, or timeouts (#11), and `_make_request` calls
`response.json()` unconditionally so any empty-bodied response (notably DELETE)
raises (#11). Check the relevant issue before assuming a capability exists.

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
