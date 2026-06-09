#!/usr/bin/env python3
"""Print a content fingerprint of archive.db.

``crawl.py`` refreshes a month with DELETE + re-INSERT, which reassigns
rowids and reshuffles the physical SQLite layout -- so the *file* bytes
change even when no message did. Committing on file bytes would therefore
produce a 26 MB commit every CI run. This hashes message *content* only
(order-independent XOR of per-row hashes), so CI commits the DB only when
messages truly change.

    python3 dbhash.py [archive.db]
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys

COLS = ("month", "msgid", "in_reply_to", "subject", "from_name",
        "from_email", "date_iso", "body", "source", "body_html", "thread_id",
        "archive_source", "source_file")   # provenance: corrections must republish


def fingerprint(db: str) -> str:
    con = sqlite3.connect(db)
    acc = 0
    have = {r[1] for r in con.execute("PRAGMA table_info(message)")}
    cols = [c for c in COLS if c in have]   # tolerate a DB predating a column
    for row in con.execute(f"SELECT {', '.join(cols)} FROM message"):
        digest = hashlib.sha256(repr(row).encode("utf-8", "replace")).digest()
        acc ^= int.from_bytes(digest, "big")
    # fold in attachments by url + filename + content_type + sha256(content):
    # url+size alone collide when a same-length payload changes (e.g. an
    # attachment is re-sanitised), so a real content change would not republish.
    if con.execute("SELECT name FROM sqlite_master "
                   "WHERE type='table' AND name='attachment'").fetchone():
        for url, fn, ct, content in con.execute(
                "SELECT url, filename, content_type, content FROM attachment"):
            ch = hashlib.sha256(content).hexdigest() if content is not None else ""
            digest = hashlib.sha256(
                repr((url, fn, ct, ch)).encode("utf-8", "replace")).digest()
            acc ^= int.from_bytes(digest, "big")
    con.close()
    return f"{acc:064x}"


if __name__ == "__main__":
    print(fingerprint(sys.argv[1] if len(sys.argv) > 1 else "archive.db"))
