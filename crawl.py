#!/usr/bin/env python3
"""Crawl the Xymon Pipermail archive into SQLite.

Source of truth is the per-month gzipped mbox each Mailman/Pipermail archive
exposes at ``xymon/<YYYY-Month>.txt.gz`` -- far cleaner than scraping HTML.
"""
from __future__ import annotations

import argparse
import email
import re
import sqlite3
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Iterator

import mailstore

BASE = "https://lists.xymon.com/"
LIST = "xymon"
UA = "xymon-discussion-public/1.0 (+stdlib crawler)"
_MAX_FETCH = 100 * 1024 * 1024     # cap a single download (compressed)
_MAX_GUNZIP = 500 * 1024 * 1024    # cap the decompressed mbox (gzip-bomb guard)

# An mbox "From " envelope line ends with an asctime "HH:MM:SS YYYY", and a
# real separator sits at the start of the file or after a blank line (the
# blank-line rule rejects forwarded "From ..." lines quoted inside a body).
# Matching this ourselves (rather than mailbox.mbox) also avoids its ASCII
# decode of the From_ line, which crashes on non-ASCII sender names and would
# silently drop an entire month.
_FROM = re.compile(
    rb"(?m)(?:\A|(?<=\n\n))From .+\d{2}:\d{2}:\d{2} \d{4}\s*?$")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(_MAX_FETCH + 1)
    if len(data) > _MAX_FETCH:
        raise ValueError(f"response exceeds {_MAX_FETCH} bytes: {url}")
    return data


def _gunzip_bounded(data: bytes, limit: int = _MAX_GUNZIP) -> bytes:
    """Decompress a gzip stream, aborting once output would exceed `limit` -- a
    crafted (or corrupted) .txt.gz can't expand to gigabytes and OOM the runner
    before a size check fires (the SSRF/cap hardening parity for the crawler)."""
    d = zlib.decompressobj(31)                 # 16 + MAX_WBITS -> gzip framing
    out = bytearray(d.decompress(data, limit + 1))
    while d.unconsumed_tail and len(out) <= limit:
        out += d.decompress(d.unconsumed_tail, limit + 1 - len(out))
    if len(out) > limit:
        raise ValueError(f"gzip expands beyond {limit} bytes")
    out += d.flush()
    if len(out) > limit:
        raise ValueError(f"gzip expands beyond {limit} bytes")
    return bytes(out)


def list_months() -> list[str]:
    """Return month archive names, e.g. ['2024-January', ...]."""
    html = fetch(BASE).decode("utf-8", "replace")
    seen: list[str] = []
    for m in re.findall(rf'href="{LIST}/([^/"]+)/thread\.html"', html, re.I):
        if m not in seen:
            seen.append(m)
    return seen


def _iter_mbox(raw: bytes) -> Iterator[tuple[bytes, email.message.Message]]:
    """Split raw mbox bytes; yield (chunk, message).

    ``chunk`` is the full original mbox entry (From_ line through the next
    separator) -- kept verbatim so the month's mbox can be regenerated for
    download. The parsed message uses the payload with mboxrd unescaping.
    """
    starts = [m.start() for m in _FROM.finditer(raw)]
    if not starts:
        return
    starts.append(len(raw))
    for i in range(len(starts) - 1):
        chunk = raw[starts[i]:starts[i + 1]]
        nl = chunk.find(b"\n")                       # drop the From_ envelope
        payload = chunk[nl + 1:] if nl != -1 else b""
        payload = re.sub(rb"(?m)^>(>*From )", rb"\1", payload)   # mboxrd unescape
        yield chunk, email.message_from_bytes(payload)


def parse_month(month: str) -> Iterator[dict]:
    """Download and parse one month's mbox into message dicts."""
    raw = _gunzip_bounded(fetch(f"{BASE}{LIST}/{month}.txt.gz"))
    for chunk, msg in _iter_mbox(raw):
        row = mailstore.message_to_row(msg, month=month, raw=chunk)
        row["raw"] = chunk
        yield row


def store(conn: sqlite3.Connection, month: str, rows: list[dict]) -> int:
    # Re-fetch replaces the month so the current month can grow. Returns the
    # number actually stored -- duplicate Message-Ids are dropped, matching
    # Pipermail, so this is below the parsed count for months with dupes.
    # Only delete this crawler's own rows so imported sources (e.g. an mbox
    # import) sharing the month are preserved.
    conn.execute("DELETE FROM message WHERE month = ? AND source = 'list'",
                 (month,))
    return mailstore.insert_rows(conn, rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl Xymon archive into SQLite")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--limit", type=int, default=0,
                    help="only crawl N most recent months (0 = all)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between requests (be polite)")
    args = ap.parse_args()

    conn = mailstore.connect(args.db)

    months = list_months()
    if args.limit:
        months = months[: args.limit]
    print(f"{len(months)} month(s) to crawl")

    total = 0
    for i, month in enumerate(months, 1):
        try:
            rows = list(parse_month(month))
            n = store(conn, month, rows)
            total += n
            print(f"[{i}/{len(months)}] {month}: {n} messages")
        except Exception as exc:  # noqa: BLE001  keep crawling on a bad month
            print(f"[{i}/{len(months)}] {month}: ERROR {exc}")
        time.sleep(args.delay)

    print(f"Done. {total} messages in {args.db}")
    conn.close()


if __name__ == "__main__":
    main()
