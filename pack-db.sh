#!/usr/bin/env sh
# Repack archive.db -> archive.db.gz, but only when message *content* changed
# (crawl.py churns file bytes every run; dbhash.py fingerprints content).
# Prints "changed" when it repacked, "unchanged" otherwise. Host-agnostic:
# the git commit/push is left to the caller (workflow), so this works under
# any CI or a plain cron without GitHub specifics.
set -eu
cd "$(dirname "$0")"

DB="${DB:-archive.db}"
GZ="${GZ:-archive.db.gz}"

NEW=$(python3 dbhash.py "$DB")
OLD=""
if [ -f "$GZ" ]; then
  tmp=$(mktemp)
  gunzip -c "$GZ" > "$tmp"
  OLD=$(python3 dbhash.py "$tmp")
  rm -f "$tmp"
fi

if [ "$NEW" = "$OLD" ]; then
  echo "unchanged"
else
  gzip -9 -n -c "$DB" > "$GZ"
  echo "changed"
fi
