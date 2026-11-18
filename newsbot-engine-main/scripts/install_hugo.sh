#!/usr/bin/env bash
# Install Hugo extended with retries. GitHub's release CDN sometimes answers 504 for a minute; the old action gave up after 3 tries
# and failed the whole run. This retries for ~5 minutes. Usage: scripts/install_hugo.sh 0.139.0
set -euo pipefail
V="${1:?hugo version}"
URL="https://github.com/gohugoio/hugo/releases/download/v${V}/hugo_extended_${V}_linux-amd64.tar.gz"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsSL --retry 3 --retry-all-errors --connect-timeout 20 -o /tmp/hugo.tgz "$URL" && tar -xzf /tmp/hugo.tgz -C /tmp hugo; then
    sudo install -m 0755 /tmp/hugo /usr/local/bin/hugo
    hugo version
    exit 0
  fi
  echo "hugo download attempt $i failed; waiting"; sleep $((i * 6))
done
echo "could not download Hugo" >&2
exit 1
