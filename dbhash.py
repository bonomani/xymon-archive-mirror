#!/usr/bin/env python3
"""Print a content fingerprint of archive.db.

``crawl.py`` refreshes a month with DELETE + re-INSERT, which reassigns
rowids and reshuffles the physical SQLite layout -- so the *file* bytes
change even when no message did. Committing on file bytes would therefore
produce a 26 MB commit every CI run. This hashes message *content* only
(order-independent), so CI commits the DB only when messages truly change.

The accumulator is a SUM (mod 2**256), not an XOR: XOR cancels any pair of
byte-identical contributions to zero, so two rows that hash the same -- e.g.
two NULL-msgid rows with identical content, which no UNIQUE constraint
forbids -- would vanish from the fingerprint and a real content change could
fail to republish. Addition is equally order-independent but does not cancel.
Each row's fields are length-prefixed and type-tagged before hashing so a
value cannot drift across a field boundary (the old repr() form could).

    python3 dbhash.py [archive.db]
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys

_MOD = 1 << 256

COLS = ("month", "msgid", "in_reply_to", "subject", "from_name",
        "from_email", "date_iso", "body", "source", "body_html", "thread_id",
        "archive_source", "source_file",   # provenance: corrections must republish
        # `raw` IS published -- it ships in archive.db.gz and generate.py builds
        # the downloadable per-month mbox from it -- and obfuscate.py rewrites it
        # (address pseudonymisation, incl. addresses decoded out of base64 / QP
        # MIME parts). It MUST be fingerprinted: when it was omitted, a privacy
        # scrub that only touched `raw` left the fingerprint unchanged, so pack-db
        # saw "no change" and never republished -- the corrected DB never shipped.
        "raw")


def _row_digest(values) -> int:
    """Order-unambiguous SHA-256 of a row's fields. Each field is tagged by type
    (None / bytes / scalar) and length-prefixed, so ('a','bc') and ('ab','c')
    -- equal under repr() concatenation -- hash differently, and None is distinct
    from the string 'None'."""
    h = hashlib.sha256()
    for v in values:
        if v is None:
            h.update(b"N\x00")
        elif isinstance(v, (bytes, bytearray)):
            b = bytes(v)
            h.update(b"B" + len(b).to_bytes(8, "big") + b)
        else:
            b = str(v).encode("utf-8", "surrogatepass")
            h.update(b"S" + len(b).to_bytes(8, "big") + b)
    return int.from_bytes(h.digest(), "big")


def fingerprint(db: str) -> str:
    con = sqlite3.connect(db)
    acc = 0
    have = {r[1] for r in con.execute("PRAGMA table_info(message)")}
    cols = [c for c in COLS if c in have]   # tolerate a DB predating a column
    for row in con.execute(f"SELECT {', '.join(cols)} FROM message"):
        acc = (acc + _row_digest(row)) % _MOD
    # fold in attachments by url + filename + content_type + sha256(content):
    # url+size alone collide when a same-length payload changes (e.g. an
    # attachment is re-sanitised), so a real content change would not republish.
    if con.execute("SELECT name FROM sqlite_master "
                   "WHERE type='table' AND name='attachment'").fetchone():
        for url, fn, ct, content in con.execute(
                "SELECT url, filename, content_type, content FROM attachment"):
            ch = hashlib.sha256(content).hexdigest() if content is not None else None
            acc = (acc + _row_digest((url, fn, ct, ch))) % _MOD
    con.close()
    return f"{acc:064x}"


if __name__ == "__main__":
    print(fingerprint(sys.argv[1] if len(sys.argv) > 1 else "archive.db"))
