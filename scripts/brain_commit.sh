#!/usr/bin/env bash
# Commit engine output to the brain repo checkout. Prints nothing but short status words (public Actions logs).
# usage: brain_commit.sh <brain_dir> auto|review|state
#   auto   state + pending + content go to main in one commit
#   review state + pending go to main; new content goes to a review/<stamp> branch and a pull request
#          (merging the PR on a phone = publish). The working tree is switched back to main afterwards so the
#          Hugo build never contains unapproved articles.
#   state  only state/ (used after a deploy to record the content fingerprint)
# Needs: git, gh (preinstalled on GitHub runners), GH_TOKEN with contents + pull requests write on the brain repo.
set -euo pipefail
BRAIN_DIR="${1:?brain dir}"; MODE="${2:?mode}"
BRANCH_MAIN="${BRAIN_BRANCH:-main}"
STAMP="$(date -u +%Y%m%d-%H%M)"
cd "$BRAIN_DIR"
git config user.name "newsbot"
git config user.email "newsbot@users.noreply.github.com"

push_main() {   # push HEAD to main; on a race (owner merged a PR meanwhile) rebase and retry
  for _ in 1 2 3; do
    if git push -q origin "HEAD:${BRANCH_MAIN}" 2>/dev/null; then return 0; fi
    git fetch -q --depth=50 origin "${BRANCH_MAIN}"
    git rebase -q "origin/${BRANCH_MAIN}" || { git rebase --abort || true; echo "push-failed"; return 1; }
  done
  echo "push-failed"; return 1
}

case "$MODE" in
  auto)   git add state pending content ;;
  review) git add state pending ;;
  state)  git add state ;;
  *) echo "bad-mode"; exit 2 ;;
esac
if ! git diff --cached --quiet; then
  git commit -q -m "newsbot: ${MODE} ${STAMP}"
  push_main
  echo "committed"
else
  echo "nothing-to-commit"
fi

if [ "$MODE" = "review" ] && [ -n "$(git status --porcelain -- content)" ]; then
  BRANCH="review/${STAMP}"
  git checkout -q -b "$BRANCH"
  git add content
  git commit -q -m "newsbot: review ${STAMP}"
  git push -q origin "$BRANCH" 2>/dev/null
  gh pr create --base "$BRANCH_MAIN" --head "$BRANCH" --title "Review: new articles ${STAMP}" \
    --body "Merge to publish. Close without merging to reject." >/dev/null
  git checkout -q "$BRANCH_MAIN"
  echo "pr-opened"
fi
