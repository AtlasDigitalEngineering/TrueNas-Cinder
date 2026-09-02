#!/usr/bin/env bash
# Count everything the review app has published on one PR (#38).
#
# Three surfaces, because the reviewer uses more than one: a summary is an
# issue comment, an inline finding is a pull-request review comment, and a
# formal review is a third thing again. Counting only the first would call a
# run that posted inline findings "silent".
#
# Reads GH_TOKEN, REPO and PR from the environment. Prints one integer.
set -euo pipefail

# The GitHub App's identity. Matched on login rather than id so that
# reinstalling the app does not silently make this count zero forever --
# a rename would fail loudly on the next silent run instead.
APP='claude[bot]'

count_from() {
  gh api --paginate "repos/${REPO}/$1" \
    --jq "[.[] | select(.user.login == \"${APP}\")] | length" 2>/dev/null \
    | awk '{total += $1} END {print total + 0}'
}

issue_comments=$(count_from "issues/${PR}/comments")
review_comments=$(count_from "pulls/${PR}/comments")
reviews=$(count_from "pulls/${PR}/reviews")

echo $((issue_comments + review_comments + reviews))
