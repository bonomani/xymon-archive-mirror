#!/usr/bin/env python3
"""Recover HTML-only message bodies that Pipermail scrubbed to an attachment.

Some senders posted HTML with no plain-text part. Pipermail stripped the HTML
to an external ``attachment.html`` and left the body as just a scrub note, so
the message looks empty. For those (and only those -- where the real body is
empty) we fetch the attachment.html, sanitize it, and store it as body_html.

Idempotent: skips messages that already have body_html. Run before
obfuscate.py so the recovered HTML gets pseudonymised too.

    python3 fetch_scrubbed_html.py
"""
from __future__ import annotations

import argparse
import html
import re
import sqlite3
import time
from pathlib import Path

import mailstore
from fetch_attachments import fix_url, httpget

_URL = re.compile(r"URL:\s*<([^>]*attachment\.html?[^>]*)>", re.S | re.I)
_NOISE = re.compile(r"(?im)^.*(scrubbed|next part|URL:|Type:|Size:|Name:|Desc).*$")


def depipermail(s: str) -> str:
    """Pipermail's attachment.html shows the email's HTML *source* escaped and
    wrapped (<tt>, <br>, &nbsp;, &lt;...). Undo that to recover the original
    HTML, ready for sanitize_html()."""
    s = re.sub(r"</?tt>", "", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = s.replace("&nbsp;", " ")
    return html.unescape(s)


def real_body_empty(body: str) -> bool:
    b = _NOISE.sub("", body or "")
    b = re.sub(r"-+\s*next part\s*-+", "", b, flags=re.I)
    return len(re.sub(r"\s+", " ", b).strip()) < 15


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Recover scrubbed HTML-only bodies")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--cache", default="sources/scrubbed_html.db", type=Path,
                    help="persistent url->html cache (skip re-fetch); '' to disable")
    ap.add_argument("--no-network", action="store_true",
                    help="restore from the cache only; never fetch (offline rebuild)")
    args = ap.parse_args(argv)

    conn = mailstore.connect(args.db)
    targets = []
    for mid, body, bhtml in conn.execute(
            "SELECT id, body, body_html FROM message "
            "WHERE body LIKE '%attachment.htm%'"):   # .htm and .html
        if bhtml:
            continue
        m = _URL.search(body or "")
        if m and real_body_empty(body):
            targets.append((mid, fix_url(re.sub(r"\s+", "", m.group(1)))))
    print(f"{len(targets)} HTML-only message(s) to recover")

    # persistent url->html cache so recovered bodies are not re-downloaded from
    # HyperKitty every rebuild (committed to the private vault = durable backup).
    cache = None
    have = {}
    if str(args.cache) and Path(args.cache).parent.exists():
        cache = sqlite3.connect(args.cache)
        cache.execute("CREATE TABLE IF NOT EXISTS html "
                      "(url TEXT PRIMARY KEY, body_html TEXT, raw_html TEXT)")
        if "raw_html" not in {r[1] for r in cache.execute(
                "PRAGMA table_info(html)")}:        # preserve the un-sanitised
            cache.execute("ALTER TABLE html ADD COLUMN raw_html TEXT")  # source
        have = dict(cache.execute("SELECT url, body_html FROM html"))

    done = cached = 0
    for mid, url in targets:
        if url in have:                       # served from the vault cache
            conn.execute("UPDATE message SET body_html=? WHERE id=?",
                         (have[url], mid))
            cached += 1
            continue
        if args.no_network:                   # offline: cache restore only
            continue
        try:
            data, hdr = httpget(url)
            raw = mailstore.decode_payload(data, hdr.get_content_charset())
            htm = mailstore.sanitize_html(depipermail(raw))
            if htm:
                conn.execute("UPDATE message SET body_html=? WHERE id=?",
                             (htm, mid))
                if cache is not None:
                    # store BOTH the rendered HTML and the raw source response,
                    # so the original is preserved (not just the derived form).
                    cache.execute("INSERT OR IGNORE INTO html "
                                  "(url, body_html, raw_html) VALUES (?, ?, ?)",
                                  (url, htm, raw))
                done += 1
                if done % 50 == 0:
                    conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {url}: {exc}")
        time.sleep(args.delay)

    conn.commit()
    conn.close()
    if cache is not None:
        if done:                  # only persist when something new was added,
            cache.commit()        # so the committed cache file is untouched on
        cache.close()             # no-op runs (no spurious git commit)
    print(f"recovered {done} HTML body/bodies (+{cached} from cache)")


if __name__ == "__main__":
    main()
