#!/usr/bin/env python3
"""Fetch new mail from an IMAP mailbox into the same SQLite store.

Incremental: remembers the last UID seen per folder in ``imap_state`` and
only pulls UIDs above it, so re-runs (e.g. from CI) append rather than
re-process. Dedup is by Message-Id, so overlap with the crawled archive is
harmless.

Credentials come from the environment so they can be CI secrets:
    IMAP_HOST, IMAP_USER, IMAP_PASSWORD  (IMAP_PORT optional, default 993)
"""
from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
import sqlite3
from pathlib import Path

import mailstore


def _uidvalidity(imap: imaplib.IMAP4, folder: str):
    """The folder's current UIDVALIDITY, or None if it can't be read. A change
    means the server renumbered UIDs, so a stored checkpoint no longer maps to
    the same messages."""
    typ, data = imap.status(folder, "(UIDVALIDITY)")
    if typ != "OK" or not data or not data[0]:
        return None
    m = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
    return int(m.group(1)) if m else None


def get_last_uid(conn: sqlite3.Connection, folder: str, host: str | None = None,
                 account: str | None = None, uidvalidity: int | None = None
                 ) -> int:
    """Stored checkpoint for ``folder`` -- but 0 (re-scan from the start) when it
    was recorded against a different UIDVALIDITY / host / account, since the old
    UIDs are then meaningless."""
    row = conn.execute(
        "SELECT last_uid, uidvalidity, host, account FROM imap_state "
        "WHERE folder=?", (folder,)).fetchone()
    if not row:
        return 0
    last_uid, suv, shost, sacct = row
    if (uidvalidity is not None and suv is not None and suv != uidvalidity) \
            or (host is not None and shost is not None and shost != host) \
            or (account is not None and sacct is not None and sacct != account):
        return 0
    return last_uid


def set_last_uid(conn: sqlite3.Connection, folder: str, uid: int,
                 host: str | None = None, account: str | None = None,
                 uidvalidity: int | None = None) -> None:
    conn.execute(
        "INSERT INTO imap_state(folder, last_uid, uidvalidity, host, account) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(folder) DO UPDATE SET last_uid=excluded.last_uid, "
        "uidvalidity=excluded.uidvalidity, host=excluded.host, "
        "account=excluded.account",
        (folder, uid, uidvalidity, host, account))
    conn.commit()


def fetch(conn: sqlite3.Connection, host: str, user: str, password: str,
          folder: str, port: int) -> tuple[int, int]:
    """Pull new messages from one IMAP folder. Returns (seen, added)."""
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(user, password)
        imap.select(folder, readonly=True)  # readonly: don't touch \Seen
        uidvalidity = _uidvalidity(imap, folder)
        last_uid = get_last_uid(conn, folder, host, user, uidvalidity)
        typ, data = imap.uid("search", None, f"{last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return 0, 0
        uids = sorted(int(u) for u in data[0].split() if int(u) > last_uid)

        # Advance the checkpoint only across a CONTIGUOUS run of successful
        # fetches: once one UID fails, stop moving the checkpoint (but keep
        # fetching the rest, which still store -- dedup by Message-Id makes the
        # next run's re-fetch harmless). The old max()-of-successes skipped a
        # failed lower UID forever.
        rows, checkpoint, blocked = [], last_uid, False
        for uid in uids:
            typ, msg_data = imap.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                blocked = True
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            rows.append(mailstore.message_to_row(msg))
            if not blocked:
                checkpoint = uid

        added = mailstore.insert_rows(conn, rows) if rows else 0
        if uids:                       # record checkpoint + mailbox identity
            set_last_uid(conn, folder, checkpoint, host, user, uidvalidity)
        return len(uids), added
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch IMAP mail into archive.db")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--host", default=os.environ.get("IMAP_HOST"))
    ap.add_argument("--user", default=os.environ.get("IMAP_USER"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("IMAP_PORT", "993")))
    args = ap.parse_args()

    password = os.environ.get("IMAP_PASSWORD")
    if not (args.host and args.user and password):
        ap.error("set IMAP_HOST, IMAP_USER, IMAP_PASSWORD (env or flags)")

    conn = mailstore.connect(args.db)
    seen, added = fetch(conn, args.host, args.user, password,
                        args.folder, args.port)
    conn.close()
    print(f"{args.folder}: {seen} new UID(s), {added} message(s) added")


if __name__ == "__main__":
    main()
