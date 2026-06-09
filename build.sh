#!/usr/bin/env sh
# Portable build: produce a static site from archive.db.gz.
#
# No GitHub/CI assumptions -- runs the same locally, under cron, GitLab CI,
# a plain server, etc. The only GitHub-specific pieces (DB commit-back and
# Pages deploy) live in .github/workflows/pages.yml, not here.
#
# Env knobs:
#   REFRESH=0            skip the network refresh (offline rebuild from the DB)
#   REFRESH_MONTHS=3     how many recent months to re-crawl (default 3)
#   DB=archive.db        working SQLite path
#   GZ=archive.db.gz     committed compressed snapshot
#   OUT=site             output directory
#   IMAP_HOST/USER/PASSWORD        enable the IMAP source
#   GH_DISCUSSIONS_REPO=owner/name enable the GitHub Discussions source
#   HYPERKITTY_URL=...             (future) enable the HyperKitty source
set -eu
cd "$(dirname "$0")"

DB="${DB:-archive.db}"
GZ="${GZ:-archive.db.gz}"
OUT="${OUT:-site}"

# 1. Obtain a working DB: decompress the committed snapshot, or full backfill.
if [ ! -f "$DB" ]; then
  if [ -f "$GZ" ]; then
    echo ">> decompress $GZ -> $DB"
    gunzip -kf "$GZ"
  else
    echo ">> no DB found; full Pipermail backfill (slow, one-time)"
    python3 crawl.py --db "$DB"
  fi
fi

# 2. Refresh from sources (REFRESH=0 for a pure offline rebuild).
if [ "${REFRESH:-1}" = "1" ]; then
  echo ">> refresh recent ${REFRESH_MONTHS:-3} month(s)"
  python3 crawl.py --db "$DB" --limit "${REFRESH_MONTHS:-3}"

  echo ">> fetch new attachments"
  python3 fetch_attachments.py --db "$DB" || echo "   (attachments step failed; continuing)"

  if [ -n "${IMAP_HOST:-}" ]; then
    echo ">> fetch IMAP mailbox"
    python3 fetch_mailbox.py --db "$DB"
  fi
  if [ -n "${GH_DISCUSSIONS_REPO:-}" ]; then
    echo ">> fetch GitHub Discussions ($GH_DISCUSSIONS_REPO)"
    python3 fetch_github_discussions.py --db "$DB" --repo "$GH_DISCUSSIONS_REPO"
  fi
fi

# 2b. Obfuscate personal email addresses before anything is published
# (idempotent; set OBFUSCATE_SALT as a CI secret for a stable, secret salt).
echo ">> obfuscate addresses"
python3 obfuscate.py "$DB"

# 3. Render the static site.
echo ">> generate $OUT/"
python3 generate.py --db "$DB" --out "$OUT"
echo ">> done: $OUT/"
