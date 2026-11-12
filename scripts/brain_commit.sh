#!/usr/bin/env bash
# Commit engine output to the brain repo checkout. Prints nothing but short status words (public Actions logs).
# usage: brain_commit.sh <brain_dir> auto|state
#   auto   state + content + rejected go to main in one commit (articles are live as soon as the site is built)
#   state  only state/ (used after a deploy to record the content fingerprint)
# Needs: git, GH_TOKEN with contents write on the brain repo.
set -euo pipefail
BRAIN_DIR="${1:?brain dir}"; MODE="${2:?mode}"
BRANCH_MAIN="${BRAIN_BRANCH:-main}"
STAMP="$(date -u +%Y%m%d-%H%M)"
cd "$BRAIN_DIR"
git config user.name "newsbot"
git config user.email "newsbot@users.noreply.github.com"

push_main() {   # push HEAD to main; on a race (another commit meanwhile) rebase and retry
  for _ in 1 2 3; do
    if git push -q origin "HEAD:${BRANCH_MAIN}" 2>/dev/null; then return 0; fi
    git fetch -q --depth=50 origin "${BRANCH_MAIN}"
    git rebase -q "origin/${BRANCH_MAIN}" || { git rebase --abort || true; echo "push-failed"; return 1; }
  done
  echo "push-failed"; return 1
}

case "$MODE" in
  auto)  git add state content; [ -d rejected ] && git add rejected || true ;;
  state) git add state ;;
  *) echo "bad-mode"; exit 2 ;;
esac
if ! git diff --cached --quiet; then
  git commit -q -m "newsbot: ${MODE} ${STAMP}"
  push_main
  echo "committed"
else
  echo "nothing-to-commit"
fi
