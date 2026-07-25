# AGENTS.md — TRUENAS-CINDER

**TrueNAS Cinder** An Openstack Cinder Driver for TrueNas Scale.

## GitOps Workflow

Every unit of work — whether it starts
as a `TaskCreate` task or is requested directly — follows this flow:

0. **Read before you write.** Before resuming or starting work on an issue,
   check its comment thread (and its PR's, if one already exists) for
   context — decisions made, blockers found, direction changes. Don't
   re-derive context that's already sitting there in comments.
1. **Issue first.** Before starting work, create a GitHub issue
   (`gh issue create`) with:
   - A concrete, specific title (not "fix bug" — name the actual thing).
   - A description covering context/why, what needs to change, and
     acceptance criteria.
   - Labels: one type (`bug`/`enhancement`/`chore`/`documentation`), one or
     more `area:*` (`area:backend`/`area:dapp`/`area:contracts`/`area:e2e`),
     plus `upstream` if it's blocked on a third-party dependency rather than
     our own code.
   - Assignee: setkeh, unless the work is fully self-contained and doesn't
     need his action to complete.
2. **Branch per issue.** `<type>/<issue-number>-<short-slug>`, e.g.
   `feat/3-stillness-gate-cache`, `bug/2-sui-test-wallet-relay`. Branch from
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
6. **Claude review runs automatically.** `.github/workflows/claude-code-review.yml`
   triggers Claude on every PR open/push, posting review comments against
   the conventions in this file. It is advisory only — it cannot approve,
   merge, or push commits. Set up via `/install-github-app`, which installs
   the Claude GitHub App and adds a repo secret named
   `CLAUDE_CODE_OAUTH_TOKEN` (not a raw `ANTHROPIC_API_KEY`) — done as of
   2026-07-24.
7. **Merge after approval**, which closes the linked issue automatically.

**Known gap**: main is *not* technically protected (GitHub blocks branch
protection/rulesets on private repos below the Pro/Team plan) — direct
pushes are still physically possible. Treat the flow above as a hard rule
regardless; if the plan is ever upgraded, enable "require PR + 1 approval,
no force-push/deletion" on main immediately.

**No AI attribution, anywhere** — not in commit messages, not in PR/issue
titles or descriptions, not in issue/PR comments. No `Co-Authored-By:
Claude`, no session links, nothing. This is a standing rule, not
per-request.

## Rules for Agents

1. **Match existing code style**: module-level doc comments, error codes starting from 0, `#[allow(unused_use)]` on extension modules
2. **Tests required** for new functions — add to the corresponding `*_tests.py` file
3. **Follow the GitOps Workflow above** for every task — issue, branch, PR, review, merge. See that section for the exact conventions.