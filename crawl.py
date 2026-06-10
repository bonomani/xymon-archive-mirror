#!/usr/bin/env python3
"""Crawl the Xymon Pipermail archive into SQLite.

Source of truth is the per-month gzipped mbox each Mailman/Pipermail archive
exposes at ``xymon/<YYYY-Month>.txt.gz`` -- far cleaner than scraping HTML.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import mailstore
import webfetch

BASE = "https://lists.xymon.com/"
LIST = "xymon"
UA = "xymon-discussion-public/1.0 (+stdlib crawler)"
_MAX_FETCH = 100 * 1024 * 1024     # cap a single download (compressed)
_MAX_GUNZIP = 500 * 1024 * 1024    # cap the decompressed mbox (gzip-bomb guard)

def fetch(url: str) -> bytes:
    """Capped GET via the shared hardened layer (webfetch)."""
    data, _ = webfetch.get(url, max_bytes=_MAX_FETCH, timeout=60, ua=UA)
    return data


def _gunzip_bounded(data: bytes, limit: int = _MAX_GUNZIP) -> bytes:
    """Bounded gunzip (gzip-bomb guard) -- shared implementation."""
    return webfetch.gunzip_bounded(data, limit)


def list_months() -> list[str]:
    """Return month archive names, e.g. ['2024-January', ...]."""
    html = fetch(BASE).decode("utf-8", "replace")
    seen: list[str] = []
    for m in re.findall(rf'href="{LIST}/([^/"]+)/thread\.html"', html, re.I):
        if m not in seen:
            seen.append(m)
    return seen


def parse_month(month: str) -> Iterator[dict]:
    """Download and parse one month's mbox into message dicts."""
    raw = _gunzip_bounded(fetch(f"{BASE}{LIST}/{month}.txt.gz"))
    for chunk, msg in mailstore.iter_mbox(raw):
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
