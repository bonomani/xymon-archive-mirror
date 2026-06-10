#!/usr/bin/env python3
"""Import a local mbox (e.g. a mail-client export of the list folder).

Fills the gap after Pipermail stopped (2024-07 onward): these are full
original emails with inline attachments, not Pipermail's scrubbed copies.
Differences from crawl.py:
  * author display name has a " via Xymon" suffix (Mailman 3 From-rewrite) -> strip it
  * attachments are inline MIME parts -> extract and store directly (no URL fetch)
  * stored as source='inbox' so a crawl refresh never deletes them

    python3 import_mbox.py 202663_mbox
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import mailstore
from fetch_attachments import KEEP

_VIA = re.compile(r"\s+via\s+Xymon\s*$", re.I)
_KEEP_CT = {"text/x-patch", "text/x-diff", "application/zip",
            "application/gzip", "application/x-tar", "application/x-gtar"}


def inline_attachments(msg, msgid, month) -> list[dict]:
    """Extract worthwhile inline attachments (same keep-filter as the rest)."""
    out, idx = [], 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if not fn and part.get_content_disposition() != "attachment":
            continue
        fn = mailstore.decode_mime(fn) if fn else "attachment"
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        ct = part.get_content_type()
        if ext not in KEEP and ct not in _KEEP_CT:
            continue
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        idx += 1
        out.append({
            "msgid": msgid, "month": month,
            "url": f"inline:{msgid}#{idx}",      # synthetic idempotency key
            "filename": fn, "content_type": ct,
            "size": len(data), "content": data,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Import a local mbox export")
    ap.add_argument("mbox", type=Path)
    ap.add_argument("--db", default="archive.db", type=Path)
    args = ap.parse_args()

    conn = mailstore.connect(args.db)
    raw = args.mbox.read_bytes()

    rows, atts = [], []
    # the shared splitter also unescapes mboxrd ">From " quoting -- this
    # importer's own copy didn't, leaving stray ">" on such lines in bodies
    for chunk, msg in mailstore.iter_mbox(raw):
        row = mailstore.message_to_row(msg, month=None)   # month from Date
        if row["from_name"]:
            row["from_name"] = _VIA.sub("", row["from_name"]).strip()
        row["source"] = "inbox"
        row["raw"] = chunk
        rows.append(row)
        if row["msgid"]:
            atts += inline_attachments(msg, row["msgid"], row["month"])

    added = mailstore.insert_rows(conn, rows)
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO attachment "
        "(msgid, month, url, filename, content_type, size, content) "
        "VALUES (:msgid,:month,:url,:filename,:content_type,:size,:content)",
        atts)
    conn.commit()
    att_added = conn.total_changes - before
    print(f"parsed {len(rows)} messages: +{added} new, "
          f"+{att_added} inline attachments")
    conn.close()


if __name__ == "__main__":
    main()
