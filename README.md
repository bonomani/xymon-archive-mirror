# Xymon Archive Mirror

Two mail sources feed one SQLite store, which renders to a static HTML site.
Stdlib only — no dependencies.

```
crawl.py            (historical Pipermail mboxes) ┐
fetch_mailbox.py    (live IMAP, ongoing)          ├─> archive.db ─> generate.py ─> site/
fetch_github_discussions.py (GitHub Discussions)  ┘
```

Every source turns a message into the same `message` row via `mailstore.py`,
so `generate.py` doesn't care where it came from. Dedup is by `msgid`
(Message-Id for mail, GraphQL node ID for GitHub), so overlapping sources are
harmless. Each row carries a `source` (`list`/`imap`/`github`); GitHub bodies
are markdown and keep their pre-rendered `body_html`, so they render natively
instead of as raw text.

## Usage

```bash
# 1. Backfill the historical archive (one-time / occasional refresh)
python3 crawl.py                 # all months -> archive.db
python3 crawl.py --limit 2       # only the N most recent months (testing)

# 2. Pull new mail from the live mailbox (incremental, CI-friendly)
export IMAP_HOST=... IMAP_USER=... IMAP_PASSWORD=...
python3 fetch_mailbox.py         # appends new UIDs since last run

# 2b. Or pull from GitHub Discussions (needs the `gh` CLI authenticated)
python3 fetch_github_discussions.py --repo owner/name

# 3. Render the static mirror
python3 generate.py              # archive.db -> site/
```

Open `site/index.html`. `crawl.py` replaces whole months (so the current
month stays current); `fetch_mailbox.py` only adds messages above the last
UID seen, tracked per folder in the `imap_state` table.

## Layout

| File | Purpose |
|------|---------|
| `mailstore.py` | schema + `email.Message` -> row helpers (shared) |
| `crawl.py` | download + parse Pipermail mboxes into SQLite |
| `fetch_mailbox.py` | fetch new mail from IMAP into SQLite |
| `fetch_github_discussions.py` | fetch GitHub Discussions (via `gh`) into SQLite |
| `fetch_attachments.py` | mirror useful attachments (code/patches/archives) into SQLite |
| `import_mbox.py` | import a local mbox export (fills the post-Pipermail gap) |
| `generate.py` | render `site/` (index, search, per-month date/thread/subject/author + mbox.gz, per-message) |
| `build.sh` | portable build: DB → `site/` (no GitHub/CI assumptions) |
| `pack-db.sh` | repack `archive.db.gz` only when message content changed |
| `obfuscate.py` | replace personal emails with irreversible pseudonyms |
| `dbhash.py` | content fingerprint of the DB (gates CI commits) |
| `archive.db.gz` | the SQLite store, committed compressed (~26 MB) |
| `archive.db` | uncompressed working copy (git-ignored) |
| `site/` | generated static mirror (generated, git-ignored) |

## Portability (not tied to GitHub)

The build is a plain script, so it runs anywhere — locally, cron, GitLab CI,
a server:

```bash
./build.sh                 # decompress DB, refresh recent months, generate site/
REFRESH=0 ./build.sh       # offline rebuild from the committed DB (no network)
REFRESH_MONTHS=6 ./build.sh
python3 -m http.server -d site 8000   # preview locally
```

`.github/workflows/pages.yml` is only a thin wrapper: it calls `./build.sh`,
then does two GitHub-specific things — commit the refreshed DB back and deploy
`site/` to Pages. To host elsewhere, keep `build.sh`/`pack-db.sh` and replace
just the deploy step (e.g. `rsync site/ host:/var/www`, Netlify, S3). Live
sources are enabled by env vars (`IMAP_HOST…`, `GH_DISCUSSIONS_REPO`), not
hard-coded.

## Coverage

The Pipermail crawl covers **233 of the 240** months the source index lists
(~48k messages); a local mbox import (`import_mbox.py`, source `inbox`) extends
it through **2026** for the period after Pipermail stopped. The remaining
Pipermail gaps (2023-June/July, 2024-Aug–Dec) are dead links in the upstream index
itself — their pages return 404 at lists.xymon.com — so they are not
reproducible from any source. The mbox parser is a custom byte-level splitter
rather than `mailbox.mbox`, which crashes on non-ASCII sender lines and would
silently drop whole months.

## Attachments

Pipermail scrubs attachments out of the mbox, leaving a note linking an
external URL (under a stale `/pipermail/...` path that must be rewritten).
`fetch_attachments.py` mirrors only the **useful** types — source, patches,
scripts, archives, configs — and skips the noise: HTML re-renders (~83%, just
the body again), images, vcf, and S/MIME/PGP signatures. That trims ~23k
references / ~250 MB down to ~420 files / ~3 MB, stored as blobs in the DB and
served from `site/att/`.

## mbox export

`crawl.py` keeps each message's original mbox entry bytes (`raw` column), so
`generate.py` regenerates a downloadable `site/<month>.txt.gz` per month
(linked from each month page). It is a faithful mbox of the **archived**
(deduped) messages with full original headers — not byte-identical to the
source `.txt.gz`, which still contains the duplicate-Message-Id copies that
Pipermail's HTML and this mirror both drop.

## Privacy / address obfuscation

`build.sh` runs `obfuscate.py` before generating, so nothing published ever
contains a real address. Every email address (in `from_name`, `from_email`,
`subject`, `body`, `raw`/mbox export, Message-Ids, and text attachments) is
replaced with a stable pseudonym `user-<hash>@xymon.invalid`, where
`hash = sha256(salt + address)`. The mapping is:

- **stable** — one person always maps to one pseudonym (threading by
  Message-Id and "from the same sender" still work), and
- **irreversible** — you can't recover an address without the salt.

`@xymon.com` list/infrastructure addresses are kept. The pass is idempotent.

**Salt:** read from `$OBFUSCATE_SALT`, else `private/salt.txt` (git-ignored),
else a weak built-in default. For consistent, irreversible pseudonyms in CI,
**add `OBFUSCATE_SALT` as a GitHub Actions secret** (value = your
`private/salt.txt`) and back that file up — it's the only thing that ties
pseudonyms to addresses, and it is never committed.

## Data persistence

The expensive full Pipermail import (240 months) is done **once** and the
result committed as `archive.db.gz`. CI decompresses it, tops up only recent
months (and any live source), regenerates the site, and re-commits the `.gz`
**only when message content changed**. Re-crawling rewrites the SQLite file
even when nothing changed (DELETE+INSERT churns rowids), so the commit is
gated on `dbhash.py` -- an order-independent fingerprint of message content,
not file bytes. The commit carries `[skip ci]` to avoid a trigger loop. No
full re-crawl per run.

## Schema

`message(id, month, msgid, in_reply_to, subject, from_name, from_email,
date_iso, date_raw, body)`, unique on `(month, msgid)`.
