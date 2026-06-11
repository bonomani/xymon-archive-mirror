# Xymon Archive Mirror

Public pipeline code and static-site renderer for the Xymon mailing-list
archive. Production data comes from the private source vault, is
pseudonymised there, and crosses into this repository only as
`archive.db.gz`.

```
private raw mboxes -> rebuild + obfuscate -> data:archive.db.gz
                                                |
public main (pipeline code) + data branch DB -> generate.py -> site/ -> Pages
```

Every source turns a message into the same `message` row via `mailstore.py`,
so `generate.py` doesn't care where it came from. Dedup is by `msgid`
(Message-Id for mail, GraphQL node ID for GitHub), so overlapping sources are
harmless. Production list rows retain provider provenance separately as
`archive_source` (`pipermail` or `hyperkitty`) and `source_file`.

`main` contains the canonical code. The current public database lives in a
single force-pushed commit on the orphan `data` branch, alongside only the
small `pages.yml` workflow bootstrap needed for a data push to start Actions.
The workflow then checks out its actual build code from `main`.

## Build the published archive locally

```bash
git fetch origin data
git show origin/data:archive.db.gz > archive.db.gz
REFRESH=0 ./build.sh
python3 -m http.server -d site 8000
```

The crawler/import tools remain available for development and alternate
deployments, but the production archive is rebuilt from raw sources in the
private repository. Python pipeline code uses the standard library; UI tests
use Node.js.

## Layout

| File | Purpose |
|------|---------|
| `mailstore.py` | schema + `email.Message` -> row helpers (shared) |
| `crawl.py` | download + parse Pipermail mboxes into SQLite |
| `fetch_mailbox.py` | fetch new mail from IMAP into SQLite |
| `fetch_github_discussions.py` | fetch GitHub Discussions (via `gh`) into SQLite |
| `fetch_attachments.py` | mirror useful attachments (code/patches/archives) into SQLite |
| `fetch_scrubbed_html.py` | recover Pipermail HTML-only bodies; privately cache sanitized, decoded, and byte-exact source forms |
| `import_mbox.py` | import a local mbox export (fills the post-Pipermail gap) |
| `generate.py` | render `site/` (index, search, per-month date/thread/subject/author + mbox.gz, per-message) |
| `pagelib.py` | page chrome (CSS/JS, hashed asset names) + shared HTML/list-row helpers |
| `search_index.py` | search page template + search-index.json / body-index.json.gz writers |
| `month_pages.py` | per-month pages, accordion fragments and downloadable mbox.gz |
| `build.sh` | portable build: DB → `site/` (no GitHub/CI assumptions) |
| `pack-db.sh` | repack `archive.db.gz` only when message content changed |
| `obfuscate.py` | replace personal emails with irreversible pseudonyms |
| `dbhash.py` | content fingerprint of the DB (gates CI commits) |
| `data:archive.db.gz` | published obfuscated SQLite store; the branch also carries `pages.yml` as a trigger bootstrap |
| `archive.db` | uncompressed working copy (git-ignored) |
| `site/` | generated static mirror (generated, git-ignored) |

## Portability (not tied to GitHub)

The build is a plain script, so it runs anywhere — locally, cron, GitLab CI,
a server:

```bash
./build.sh                 # decompress DB, optionally refresh, generate site/
REFRESH=0 ./build.sh       # offline render from archive.db.gz
REFRESH_MONTHS=6 ./build.sh
python3 -m http.server -d site 8000   # preview locally
```

`.github/workflows/pages.yml` is only a thin wrapper: it calls `./build.sh`,
after fetching `archive.db.gz` from `data`, and deploys `site/` to Pages. It
does not crawl or commit database changes. To host elsewhere, keep
`build.sh`/`generate.py` and replace the deploy step (for example `rsync`,
Netlify, or S3).

## Coverage

The private vault currently contributes 233 historical Pipermail files and 19
HyperKitty monthly files. After overlap deduplication, the published database
contains 48,501 messages across 251 archive months. HyperKitty supplies the
post-Pipermail period and its richer copy wins any overlapping Message-Id.
Every published row records which provider and raw source file supplied it.

## Attachments

Pipermail scrubs attachments out of the mbox, leaving a note linking an
external URL (under a stale `/pipermail/...` path that must be rewritten).
`fetch_attachments.py` mirrors only the **useful** types — source, patches,
scripts, archives, configs — and skips the noise: HTML re-renders (~83%, just
the body again), images, vcf, and S/MIME/PGP signatures. That trims ~23k
references / ~250 MB down to ~420 files / ~3 MB, stored as blobs in the DB and
served from `site/att/`.

Original attachment bytes are retained only in the private vault, both in
SQLite and as SHA-256-addressed loose files. Before publication, text and
archive contents are inspected and sanitized; an attachment that cannot be
fully inspected or safely cleaned is withheld. The public `data` branch
contains only those derived, privacy-checked payloads.

## mbox export

`crawl.py` keeps each message's original mbox entry bytes (`raw` column), so
`generate.py` regenerates a downloadable `site/<month>.txt.gz` per month
(linked from each month page). It is a faithful mbox of the **archived**
(deduped) messages with full original headers — not byte-identical to the
source `.txt.gz`, which still contains the duplicate-Message-Id copies that
Pipermail's HTML and this mirror both drop.

## Privacy / address obfuscation

The private publisher runs `obfuscate.py` and an independent fail-closed
privacy gate before writing `data:archive.db.gz`. Every email address (in
`from_name`, `from_email`, `subject`, `body`, `raw`/mbox export, Message-Ids,
and publishable attachments) is replaced with a stable pseudonym
`user-<hash>@xymon.invalid`, where
`hash = sha256(salt + address)`. The mapping is:

- **stable** — one person always maps to one pseudonym (threading by
  Message-Id and "from the same sender" still work), and
- **irreversible** — you can't recover an address without the salt.

`@xymon.com` list/infrastructure addresses are kept. The pass is idempotent.

The production salt exists only in the private repository. Public Pages CI
receives an already-obfuscated database and never receives the salt or raw
mail.

## Data persistence

Raw source persistence belongs to the private repository. It rebuilds the
obfuscated SQLite database and uses `dbhash.py`, an order-independent content
fingerprint, to publish only real changes. Publication force-pushes one commit
containing `archive.db.gz` plus the `pages.yml` trigger bootstrap to the orphan
`data` branch, so obsolete database blobs do not accumulate in `main` history.

A `data` push starts `pages.yml`; its first checkout explicitly selects `main`,
then it fetches the new database from `data`, renders, and deploys. A `main`
push, weekly schedule, or manual workflow dispatch also renders the latest
published database.

## Schema

The central table includes message content, raw mbox bytes, provider
provenance, obfuscation state, and stable thread ID. See `mailstore.py` for the
authoritative schema.
