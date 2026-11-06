#!/usr/bin/env bash
# Local pre-push gate. A branch push runs a fast check of lint and affected tests;
# the pull request is judged by the full regression in CI, which the main-protection
# ruleset requires. A push targeting main, or HOWLPLANE_PUSH_GATE=full, runs everything.
set -uo pipefail

full="${HOWLPLANE_PUSH_GATE:-}"
pushes_content=""
# Git describes each ref as: <local ref> <local sha> <remote ref> <remote sha>.
while read -r local_ref _ remote_ref _; do
  [ "$local_ref" = "(delete)" ] && continue
  pushes_content=1
  [ "$remote_ref" = "refs/heads/main" ] && full=full
done

if [ -z "$pushes_content" ] && [ "$full" != "full" ]; then
  exit 0
fi

cd "$(git rev-parse --show-toplevel)" || exit 1
# The gate tests this checkout, whatever an inherited shell variable says.
export HOWLPLANE_HOME="$PWD"
started=$SECONDS

if [ "$full" = "full" ]; then
  echo "Pre-push: running the full gate..."
  if ! make test lint build docs; then
    echo "Regression tests failed! Push aborted."
    exit 1
  fi
  echo "Pre-push: full gate passed in $((SECONDS - started))s."
  exit 0
fi

base="$(git merge-base origin/main HEAD 2>/dev/null || git rev-parse HEAD~1 2>/dev/null || true)"
echo "Pre-push: running the fast check. For everything, use HOWLPLANE_PUSH_GATE=full."
if ! make push-check PUSH_BASE="$base"; then
  echo "Pre-push: fast check failed after $((SECONDS - started))s. Push aborted."
  exit 1
fi
echo "Pre-push: fast check passed in $((SECONDS - started))s. CI runs the full regression on the pull request."
