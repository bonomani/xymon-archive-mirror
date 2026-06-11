#!/usr/bin/env python3
"""Per-month page writer: the month listing page, its accordion fragment
(frag/<month>.html) and the downloadable per-month mbox.gz."""
from __future__ import annotations

import gzip
from pathlib import Path

from pagelib import _msg_line, e, page

def _write_month_pages(conn, out: Path, months, att_counts, has_tid,
                       htid_of=None) -> None:
    """Per-month pages + accordion fragments + downloadable mbox.gz (the
    per-message pages were dropped; see the thread pass)."""
    for m in months:
        rows = conn.execute(
            f"""SELECT id, msgid, in_reply_to, subject, from_name, from_email,
                      date_raw, date_iso{', thread_id' if has_tid else ''}
               FROM message WHERE month=?
               ORDER BY date_iso IS NULL, date_iso, id""", (m,)).fetchall()

        def flat_list(ordered) -> str:
            out_ = "".join(_msg_line(r, att_counts, htid_of) + "</li>"
                           for r in ordered)
            return f"<ul class=mlist>{out_}</ul>"

        # Single view: messages by date. The sort switcher (date / threaded /
        # by author) was removed -- only date remains. The threaded view of a
        # conversation still lives on its own thread/<id>.html page.
        content = flat_list(rows)

        # downloadable mbox: original message bytes in date order
        raws = [rb for (rb,) in conn.execute(
            "SELECT raw FROM message WHERE month=? AND raw IS NOT NULL "
            "ORDER BY date_iso IS NULL, date_iso, id", (m,)) if rb]
        mbox_link = ""
        if raws:
            mbox = b"".join(rb if rb.endswith(b"\n") else rb + b"\n"
                            for rb in raws)
            (out / f"{m}.txt.gz").write_bytes(
                gzip.compress(mbox, 9, mtime=0))   # reproducible (gzip -n)
            mbox_link = f" &middot; <a href='{e(m)}.txt.gz'>mbox.gz</a>"

        nav = (f"<p class=meta>{len(rows)} messages{mbox_link} &middot; "
               f"<a href='index.html'>&larr; index</a></p>")
        mbody = f"<h1>{e(m)}</h1>{nav}{content}{nav}"
        (out / f"{m}.html").write_text(
            page(f"Xymon {m}", mbody,
                 desc=f"Xymon mailing list archive, {m}: "
                      f"{len(rows)} messages.",
                 canon=f"{m}.html"), "utf-8")
        # accordion fragment (loaded by the month index); msg/<id>.html links
        # resolve when the fragment is injected at the site root.
        (out / "frag" / f"{m}.html").write_text(
            f"<p class=meta>{len(rows)} messages</p>{content}", "utf-8")
