#!/usr/bin/env python3
"""Mirror worthwhile attachments referenced by scrubbed Pipermail messages.

Pipermail strips attachments from the mbox and leaves a note in the body::

    -------------- next part --------------
    ... was scrubbed ...
    URL: <http://lists.xymon.com/pipermail/xymon/attachments/DATE/HASH/name>

The real file is served at ``/xymon/attachments/...`` (the embedded
``/pipermail`` path 404s) and the URL is line-wrapped. We parse those notes,
keep only useful types (code, patches, archives, configs -- not the redundant
HTML re-renders, images, or crypto signatures), download, and store the blob.

Idempotent: ``url`` is unique, already-stored URLs are skipped.

    python3 fetch_attachments.py
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path

import mailstore
import webfetch

# Worth mirroring: source, patches, scripts, archives, configs, docs/data,
# and screenshots (png/jpeg <= IMG_MAX; obfuscate.py strips their metadata
# before publish). Deliberately excluded: html/htm (re-render of the body),
# other image formats (no metadata stripper -> obfuscate would withhold),
# vcf, and bin/sig (S/MIME + PGP signatures -- noise without the payload).
KEEP = {
    "zip", "tar", "gtar", "gz", "tgz", "bz2", "patch", "diff", "obj",
    "c", "cpp", "h", "hpp", "sh", "ksh", "bash", "pl", "pm", "py", "rb",
    "ps1", "php", "sql", "txt", "cfg", "conf", "ini", "xml", "css", "json",
    "yaml", "yml", "key", "pdf", "docx", "xls", "xlsx", "csv",
    "png", "jpg", "jpeg",
}
IMG_EXTS = {"png", "jpg", "jpeg"}
IMG_MAX = 300 * 1024          # per-image ceiling: screenshots, not photo dumps
IMG_MIN = 16 * 1024           # floor for INLINE images only: signature logos,
#                               banners and pixels are tiny; a deliberately
#                               ATTACHED image is kept whatever its size.
URL_RE = re.compile(r"URL:\s*<([^>]+)>", re.S)
UA = "xymon-discussion-public/1.0 (+attachments)"

# Attachment URLs come from message bodies (attacker-influenced), so a fetch is
# locked down: only the archive host, HTTPS only, no redirects (an open redirect
# could reach an internal address), and a hard response-size cap. This keeps the
# crawler from being turned into an SSRF probe or a memory/repo-exhaustion vector.
_ALLOWED_HOSTS = frozenset(("lists.xymon.com",))
_MAX_BYTES = 25 * 1024 * 1024            # 25 MB ceiling per attachment


def httpget(url: str, timeout: int = 60):
    """GET ``url`` with our User-Agent. Returns ``(body_bytes, headers)``.

    Hardened via the shared webfetch layer: HTTPS + allowlisted host only,
    redirects refused, body capped at ``_MAX_BYTES`` (these URLs are
    attacker-influenced -- see module note)."""
    return webfetch.get(url, max_bytes=_MAX_BYTES,
                        allowed_hosts=_ALLOWED_HOSTS,
                        follow_redirects=False, timeout=timeout, ua=UA)


def fix_url(u: str) -> str:
    u = re.sub(r"\s+", "", u)            # de-wrap line-broken URLs
    u = u.replace("/pipermail/xymon/attachments/", "/xymon/attachments/")
    return u.replace("http://", "https://")


def ext_of(url: str) -> str:
    last = url.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def iter_refs(conn: sqlite3.Connection):
    """Yield (msgid, month, url) for every kept attachment reference."""
    for msgid, month, body in conn.execute(
            "SELECT msgid, month, body FROM message "
            "WHERE body LIKE '%attachments/%'"):
        for m in URL_RE.finditer(body or ""):
            url = fix_url(m.group(1))
            if ext_of(url) in KEEP:
                yield msgid, month, url


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Mirror useful attachments")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--limit", type=int, default=0, help="max downloads")
    ap.add_argument("--delay", type=float, default=0.2)
    args = ap.parse_args(argv)

    conn = mailstore.connect(args.db)
    have = {u for (u,) in conn.execute("SELECT url FROM attachment")}
    refs = {url: (msgid, month)
            for msgid, month, url in iter_refs(conn) if url not in have}
    todo = list(refs.items())
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} new attachment(s) to fetch "
          f"({len(have)} already stored)")

    added = 0
    for i, (url, (msgid, month)) in enumerate(todo, 1):
        try:
            data, hdr = httpget(url)
            ctype = hdr.get("Content-Type", "").split(";")[0].strip()
            # Pipermail scrub-URLs carry no disposition, so the inline floor
            # applies too: tiny images there are signature decoration.
            if ext_of(url) in IMG_EXTS and not (IMG_MIN <= len(data) <= IMG_MAX):
                print(f"  - image outside {IMG_MIN // 1024}-{IMG_MAX // 1024} "
                      f"KB bounds, skipped: {url}")
                continue
            conn.execute(
                "INSERT OR IGNORE INTO attachment "
                "(msgid, month, url, filename, content_type, size, content) "
                "VALUES (?,?,?,?,?,?,?)",
                (msgid, month, url, url.rsplit("/", 1)[-1], ctype,
                 len(data), data))
            added += 1
            if i % 50 == 0:
                conn.commit()
                print(f"  [{i}/{len(todo)}] {added} stored")
        except Exception as exc:  # noqa: BLE001  keep going on a bad URL
            print(f"  ! {url}: {exc}")
        time.sleep(args.delay)

    conn.commit()
    total, nbytes_ = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM attachment").fetchone()
    conn.close()
    print(f"Done. +{added} this run; {total} attachments, "
          f"{nbytes_/1e6:.2f} MB total")


if __name__ == "__main__":
    main()
