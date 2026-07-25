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
truenas_cinder_driver/
  __init__.py      # exports TrueNASClient, __version__ ("0.1.0")
  api_client.py    # TrueNASClient — thin REST wrapper over the TrueNAS v2.0 API
  driver.py        # TrueNASISCSIDriver — NOT on main yet, see "Current State"
tests/
  unit/
    __init__.py
    test_api_client.py
docs/PLANNING.md   # milestones + issue map
tox.ini            # envlist = py39, flake8, coverage
requirements.txt   # cinder>=24.0.0, requests>=2.31.0
test-requirements.txt  # pytest, coverage, flake8, tox
```

There is **no** `setup.py` / `pyproject.toml` / `setup.cfg` — the package is
not installable, so tests only import `truenas_cinder_driver` because the repo
root happens to be on `sys.path`. There is no `.github/` directory at all.

## Commands

```bash
tox                      # full matrix (py39, flake8, coverage) — see caveats
tox -e py39              # unit tests
tox -e flake8            # lint: flake8 truenas_cinder_driver tests/
python -m pytest tests/  # direct test run
```

**Environment caveats on this machine** (verified 2026-07-25):
- `python3` is 3.14; `python3 -m venv` fails — `python3-venv` is not installed.
- `pytest`, `flake8`, `tox`, and `cinder` are **not** installed system-wide.
  Only `requests` is available. Tests can be run with
  `python3 -m unittest tests.unit.test_api_client` as a fallback.
- `python3 -m unittest discover -s tests` fails: `tests/` has no `__init__.py`
  (only `tests/unit/` does). Discovery needs `-t .` plus that missing file.
- `tox -e py39` will fail as written: the command is
  `pytest --cov=... --cov-report=xml` but `pytest-cov` is **not** in
  `test-requirements.txt`.

## Current State (as of 2026-07-25)

**Merged:** PR #6 (planning docs), PR #7 (API client + its unit tests).

**Issues #1–#5 are all still open** even though #1 and #2 are substantially
done — PRs #6 and #7 did not carry `Closes #N`, so nothing auto-closed. Close
them via a PR, not manually (see workflow below).

**In flight:** remote branch `origin/feature/driver-core` carries two unmerged
commits (`ce4c543`, `4d05411`) adding `truenas_cinder_driver/driver.py` (449
lines) and `tests/unit/test_driver.py` (120 lines). **No PR was ever opened
for it.** This is the issue #3 work. Treat it as a draft: several methods are
explicit placeholders and it does not import cleanly (see below).

### Known defects — do not assume existing code works

These are verified, not speculative. Fix them as part of whatever you touch.

**All 5 unit tests in `tests/unit/test_api_client.py` error out.** They patch
`truenas_cinder_driver.api_client.requests.Session`, but `setUp()` constructs
the client *before* the `@patch` decorator activates, so `self.client.session`
is a real `requests.Session` and the tests attempt live DNS/HTTP to
`truenas.example.com`. They also assert `session.get(...)` / `session.post(...)`
while `_make_request` calls `session.request(method, url)` — so even with
working patches the assertions would fail. Patch the instance's `session`
attribute (or build the client inside the patched test), and assert against
`session.request`.

**`api_client.py` gaps** vs. what issue #2 asks for:
- No token authentication — only HTTP basic (`session.auth = (user, pass)`).
- No 401/500-specific handling and no custom exception types; only
  `raise_for_status()`, which surfaces raw `requests` exceptions to callers.
- No request timeout and no retry policy.
- No `get_zvol_list()` — but `driver.py` calls it.
- No zvol resize/update method — needed for `extend_volume()`.
- `_make_request` is annotated `-> Dict[str, Any]` but list endpoints return a
  list.
- Endpoint paths need verification against real TrueNAS Scale 24.x/25.x:
  `create_zvol`/`delete_zvol` use `/zfs/zvol`, but modern TrueNAS manages zvols
  through `/pool/dataset` with `"type": "VOLUME"`. Confirm before building on
  this.

**`driver.py` (on `feature/driver-core`) defects:**
- `self.verify_ssl = kwargs.get('verify_ssl', False).lower() != 'false'` calls
  `.lower()` on a bool — `__init__` always raises `AttributeError`.
- Calls `self.client.get_zvol_list()`, which does not exist.
- Treats API responses as objects (`zvol.name`, `zvol.id`, `snap.dataset_name`)
  when the client returns plain dicts.
- Reads config from `kwargs` and `os.environ`. Cinder drivers must declare
  `oslo_config` opts and read them via `self.configuration.safe_get(...)`.
- `initialize_connection()` returns hardcoded/mock data and **leaks the TrueNAS
  admin password as the CHAP password**. Must be replaced with real CHAP
  credentials and real target/extent lookup.
- `create_volume_from_snapshot()`, `extend_volume()`, `terminate_connection()`,
  `ensure_export()`, `remove_export()`, `get_volume_stats()` return placeholder
  values (including hardcoded 10240/5120 GB capacity).
- Raises bare `Exception` throughout instead of `cinder.exception.*`.
- `tests/unit/test_driver.py` has the same patch-ordering bug as the API client
  tests, and patches `api_client.TrueNASClient` while the driver imports the
  symbol locally inside `__init__`, so the patch never applies.

**Doc drift to fix when convenient:**
- `CONTRIBUTING.md` tells contributors to `pip install -r requirements-dev.txt`
  — that file does not exist; it is `test-requirements.txt`.
- `README.md`'s repository-structure block lists `driver.py`, which is not on
  `main`.

## Open Issues

Issues **#1–#5** are the original umbrellas. Each carries an audit comment
mapping it to the detailed spec; keep them open as trackers and do the work in
the specific issues below.

| # | Umbrella | State |
|---|---|---|
| 1 | Project Structure and Architecture | Delivered by PR #6; packaging + `tests/__init__.py` outstanding |
| 2 | API Client | Partially delivered by PR #7; wrong endpoints, wrong auth, no error handling |
| 3 | Cinder Driver Core Logic | Draft on `feature/driver-core`, no PR, does not import |
| 4 | Testing Framework and CI/CD | Not started — no `.github/`, existing tests broken |
| 5 | Documentation and User Guide | Not started |

**M1 — minimum viable attach:** #8 (fix test mocks), #9 (`/pool/dataset`
endpoints), #10 (API-key auth), #11 (error handling/retry/timeouts), #12 (iSCSI
pipeline methods), #14 (oslo_config + `SanISCSIDriver`), #15 (CHAP credential
leak), #16 (mapping persistence G2), #22 (CI + branch protection), #26
(`feature/driver-core` disposition).

**M2 — full lifecycle:** #13 (snapshot clone/promote/pool capacity), #18
(concurrency locking G6), #21 (clone/extend/stats driver methods), #25
(functional tests).

**M3 — migration ready:** #17 (IQN/portal discovery G3+G4), #19 (snapshot
rename verification G1), #20 (`manage_existing` family), #23 (pyproject.toml),
#24 (Kolla image + deployment), #5 (documentation).

**Unscheduled:** #27 (deferred CHAP G5 + multi-attach G7), #28 (doc drift).

**Start here:** #8 first — the test suite currently provides zero signal and
everything else builds on it. Then #9/#10/#11 on the API client, since #12 and
the whole driver layer sit on top.

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
   `cinder.volume.driver.ISCSIDriver`, declare config via `oslo_config` opts
   read through `self.configuration`, and raise `cinder.exception.*` rather
   than bare `Exception`.
4. **Never log or return credentials.** No passwords in connection-info dicts,
   exception messages, or debug output.
5. **Follow the GitOps Workflow above** for every task — issue, branch, PR,
   review, merge. See that section for the exact conventions.
