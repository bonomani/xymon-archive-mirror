#!/usr/bin/env python3
"""Shared store: schema + email -> row helpers used by every source.

Both ``crawl.py`` (historical Pipermail mboxes) and ``fetch_mailbox.py``
(live IMAP) turn an :class:`email.message.Message` into the same row shape
and write to the same SQLite ``message`` table, so ``generate.py`` is source
agnostic.
"""
from __future__ import annotations

import base64
import email
import html
import re
import sqlite3
from datetime import timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

_META_CHARSET_SNIFF = 2048      # bytes of HTML scanned for a <meta charset> hint
_CID_IMG_MAX = 256_000          # max size of an inlined cid: image (data URI)


def decode_mime(s: Optional[str]) -> str:
    """Decode RFC 2047 encoded-words (e.g. =?UTF-8?Q?Henrik_St=C3=B8rner?=).

    Bogus/missing charsets ("unknown-8bit" is common in old Pipermail) are
    decoded as UTF-8 then latin-1 rather than turned into U+FFFD."""
    if not s:
        return s or ""
    try:
        parts = decode_header(s)
    except Exception:  # noqa: BLE001  malformed header -> keep as-is
        return s
    out = []
    for data, charset in parts:
        if not isinstance(data, (bytes, bytearray)):
            out.append(data)
            continue
        cs = (charset or "").lower()
        if cs and cs not in ("unknown-8bit", "x-unknown", "unknown"):
            try:
                out.append(bytes(data).decode(charset))
                continue
            except (LookupError, UnicodeDecodeError):
                pass
        try:
            out.append(bytes(data).decode("utf-8"))
        except UnicodeDecodeError:
            out.append(bytes(data).decode("latin-1", "replace"))
    return "".join(out)


# Leading reply/forward prefixes in several languages (Re/AW/Fwd/WG/SV/...),
# optionally numbered (Re[2]:), and the "[Xymon]" list tag (any case, possibly
# doubled). Stripped repeatedly so every message in a thread shows the same
# clean base subject -- the archive is inconsistent (39% of old subjects carry
# the tag, all new ones do).
_REPLY_PREFIX = re.compile(
    r"^(?:\s*(?:re|aw|fwd?|wg|sv|antwort|rif|ris|odp|vs|ynt|r)"
    r"(?:\[\d+\])?\s*:\s*)+", re.I)
# Leading noise tags to drop: the list's own names (it was "Hobbit" before
# "Xymon") plus the Exchange "external sender" marker. Content tags users typed
# -- [patch], [newbie], [bug], [devmon], ... -- are kept.
_LIST_TAG = re.compile(
    r"^\s*\[\s*(?:xymon|hobbit|external|ext|exch[^\]]*|bbwin[^\]]*)"
    r"\s*\]\s*", re.I)
# Bracketed markers that may appear anywhere (often at the end), and a leading
# "Solved:" prefix -> dropped.
_ANY_TAG = re.compile(r"\s*[\[(]\s*(?:solved|trunk)\s*[\])]\s*", re.I)
_SOLVED_PREFIX = re.compile(r"^\s*solved\s*:\s*", re.I)


def normalize_subject(s: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()   # collapse folded-header newlines
    s = _ANY_TAG.sub(" ", s).strip()           # [SOLVED]/[trunk] can sit at the end
    prev = None
    while s != prev:
        prev = s
        s = _LIST_TAG.sub("", s)
        s = _SOLVED_PREFIX.sub("", s)
        s = _REPLY_PREFIX.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip()


# --- HTML email sanitizer -------------------------------------------------
# Email HTML is untrusted (scripts, tracking pixels, arbitrary attributes), so
# we keep only a safe structural subset: layout/formatting tags, no
# attributes except a validated href, and we drop <img> (tracking beacons) and
# <script>/<style> entirely. Output is safe to drop into the static page.
_ALLOWED = {"p", "br", "ul", "ol", "li", "b", "strong", "i", "em", "u", "s",
            "a", "blockquote", "pre", "code", "h1", "h2", "h3", "h4", "h5",
            "h6", "div", "table", "thead", "tbody", "tr", "td", "th",
            "hr", "sub", "sup"}
# <span> and <div> are dropped (their text is kept). After attributes are
# stripped they are just wrappers that, mixed with <p>, cause uneven vertical
# spacing. Empty blocks and repeated <br> are removed in _tidy() so the
# spacing is uniform everywhere.
# NB: never add a literal space or \xa0 to the alternation -- \s already matches
# both, and the duplication caused catastrophic O(2^n) backtracking on long
# &nbsp; runs (sanitize_html took minutes per HTML mail).
_EMPTY_BLOCK = re.compile(r"<(p|div|blockquote)>(?:\s|&nbsp;|<br>)*</\1>")
_MULTI_BR = re.compile(r"(?:<br>\s*){2,}")
# Void elements never go on the open-tag stack (no closing tag to balance).
_VOID = {"br", "hr", "img", "wbr"}


_LEAD_BR = re.compile(r"^((?:\s*<(?:div|p|blockquote)>)*\s*)<br>\s*")
_TRAIL_BR = re.compile(r"\s*<br>(\s*(?:</(?:div|p|blockquote)>\s*)*)$")


def _tidy(s: str) -> str:
    # Collapse any run of empty blocks / repeated <br> to at most ONE blank line
    # (two <br>) -- keep the author's deliberate paragraph break, drop excess.
    # One <br> is only a line break; a blank line between paragraphs needs two.
    prev = None
    while s != prev:
        prev = s
        s = _EMPTY_BLOCK.sub("<br>", s)
    s = _MULTI_BR.sub("<br><br>", s)
    # Trim blank lines at the very top/bottom down to real text, even when the
    # content is wrapped in <div>/<p> (a leading <br> inside a wrapper still
    # shows as a blank first line).
    prev = None
    while s != prev:
        prev = s
        s = _LEAD_BR.sub(r"\1", s)
        s = _TRAIL_BR.sub(r"\1", s)
    return s.strip()
# Container tags whose *content* is removed. NOT img: it is a void element
# (no end tag), so skip-counting it would drop everything after it (e.g. a
# signature logo would swallow the rest of the message). img has no allowed
# tag, so it is simply dropped without a skip region.
_DROP_TREE = {"script", "style", "head", "title", "object", "iframe"}


class _Sanitizer(HTMLParser):
    def __init__(self, cid_map=None) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.stack: list[str] = []    # open allowed tags, to keep output balanced
        self.cid = cid_map or {}      # Content-ID -> (mimetype, bytes)

    def _img(self, attrs):
        # Render ONLY embedded cid: images (no network fetch, no tracking) as
        # self-contained data URIs. External images stay dropped.
        if self.skip:
            return
        a = dict(attrs)
        src = (a.get("src") or "")
        if src.lower().startswith("cid:"):
            part = self.cid.get(src[4:].strip().strip("<>"))
            if part:
                mime, data = part
                b64 = base64.b64encode(data).decode("ascii")
                alt = html.escape(a.get("alt") or "", quote=True)
                self.out.append(
                    f'<img src="data:{mime};base64,{b64}" alt="{alt}" '
                    'loading="lazy">')

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self._img(attrs)
            return
        if tag in _DROP_TREE:
            self.skip += 1
            return
        if self.skip or tag not in _ALLOWED:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("http://", "https://", "mailto:")):
                self.out.append(
                    f'<a href="{html.escape(href, quote=True)}" '
                    'rel="nofollow noopener" target="_blank">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag == "img":
            self._img(attrs)
        elif not self.skip and tag in _ALLOWED and tag != "a":
            # self-closed: emit balanced (void stays a single tag)
            self.out.append(f"<{tag}>" if tag in _VOID else f"<{tag}></{tag}>")

    def handle_endtag(self, tag):
        if tag in _DROP_TREE:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in _ALLOWED or tag in _VOID:
            return
        # Unwind the stack to the matching open tag, auto-closing anything left
        # improperly open inside it (so a stray </div> can't unbalance output).
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data))


def sanitize_html(s: str, cid_map=None) -> str:
    p = _Sanitizer(cid_map)
    p.feed(s)
    p.close()
    while p.stack:                    # close any tags the source left open
        p.out.append(f"</{p.stack.pop()}>")
    return _tidy("".join(p.out))


def decode_payload(payload: bytes, declared: Optional[str]) -> str:
    """Decode bytes robustly. Many list messages omit the MIME charset; honour
    the declared one, then a ``<meta charset>`` / ``charset=`` hint inside the
    payload, then utf-8, and finally cp1252 (a latin-1 superset that maps every
    byte, e.g. 0x96 -> en-dash) so we never emit U+FFFD replacement chars."""
    cands = [declared] if declared else []
    m = re.search(rb'charset=["\']?([\w-]+)', payload[:_META_CHARSET_SNIFF], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    for enc in cands + ["utf-8", "cp1252"]:
        try:
            return payload.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("cp1252", "replace")


def html_part(msg: Message) -> Optional[str]:
    """Return the sanitized HTML alternative of a message, or None.

    Embedded ``cid:`` images (<=256 KB) are inlined as data URIs so genuine
    in-message screenshots/diagrams render; external images stay stripped.
    """
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    cid_map = {}
    for part in parts:
        cid = part.get("Content-ID")
        if cid and part.get_content_type().startswith("image/"):
            data = part.get_payload(decode=True)
            if data and len(data) <= _CID_IMG_MAX:
                cid_map[cid.strip().strip("<>")] = (part.get_content_type(),
                                                    data)
    for part in parts:
        if part.get_content_type() != "text/html":
            continue
        fn = part.get_filename()
        # Accept the inline HTML alternative (no filename) OR Mailman's detached
        # HTML body: when a list scrubs HTML it re-attaches the real body as
        # "attachment.html" (Content-Disposition: attachment), which IS the
        # message -- not a genuine user-attached .html file.
        if fn and not re.match(r"attachment(-\d+)?\.html$", fn.strip(), re.I):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            text = decode_payload(payload, part.get_content_charset())
            out = sanitize_html(text, cid_map)
            # ignore an HTML part that sanitises to no real text -- it would
            # render as a blank message; fall back to the text/plain body.
            if out and re.sub(r"<[^>]+>", "", out).replace("\xa0", " ").strip():
                return out
    return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    month       TEXT NOT NULL,          -- e.g. 2024-January
    msgid       TEXT,                   -- RFC822 Message-Id / node ID (dedup key)
    in_reply_to TEXT,
    subject     TEXT,
    from_name   TEXT,
    from_email  TEXT,
    date_iso    TEXT,                   -- sortable ISO 8601, may be NULL
    date_raw    TEXT,
    body        TEXT,                   -- plain text (email) or markdown (gh)
    source      TEXT DEFAULT 'list',    -- 'list' | 'imap' | 'github'
    body_html   TEXT,                   -- pre-rendered HTML (GitHub); else NULL
    raw         BLOB,                   -- original mbox entry bytes (for export)
    UNIQUE(month, msgid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_msgid
    ON message(msgid) WHERE msgid IS NOT NULL;   -- live-fetch dedup
CREATE INDEX IF NOT EXISTS idx_message_month ON message(month);
CREATE INDEX IF NOT EXISTS idx_message_date ON message(date_iso);

CREATE TABLE IF NOT EXISTS imap_state (
    folder   TEXT PRIMARY KEY,
    last_uid INTEGER NOT NULL
);

-- Attachments were scrubbed from the Pipermail mbox and live at external
-- source URLs; fetch_attachments.py mirrors the worthwhile ones (code,
-- patches, archives, configs -- not the redundant HTML re-renders or
-- crypto signatures). Linked to its message by msgid; url is the dedup key.
CREATE TABLE IF NOT EXISTS attachment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid        TEXT,
    month        TEXT,
    url          TEXT UNIQUE,
    filename     TEXT,
    content_type TEXT,
    size         INTEGER,
    content      BLOB
);
CREATE INDEX IF NOT EXISTS idx_attachment_msgid ON attachment(msgid);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations so DBs built before new columns keep working."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN source TEXT DEFAULT 'list'")
    if "body_html" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN body_html TEXT")
    if "raw" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN raw BLOB")
    conn.commit()


def connect(db: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def apply_fast_pragmas(conn: sqlite3.Connection) -> None:
    """Trade durability for speed on a throwaway build DB (rebuilt from sources):
    no per-commit fsync/journal -- the difference is huge on a slow CI disk."""
    for pragma in ("journal_mode=MEMORY", "synchronous=OFF",
                   "temp_store=MEMORY", "cache_size=-200000"):
        conn.execute(f"PRAGMA {pragma}")


def body_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return decode_payload(payload, part.get_content_charset())
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return msg.get_payload() or ""
    return decode_payload(payload, msg.get_content_charset())


def message_to_row(msg: Message, month: Optional[str] = None,
                   raw: Optional[bytes] = None) -> dict:
    """Build a DB row from an email message.

    ``month`` is supplied by the crawler (it owns the archive partition); for
    live fetch it is derived from the Date header (e.g. ``2024-January``).
    ``raw`` (the original bytes) lets us recover a From header whose 8-bit
    bytes the bytes-parser replaced with U+FFFD by re-decoding it as UTF-8.
    """
    from_hdr = str(msg.get("From", "") or "")
    if raw and "�" in from_hdr:
        m = re.search(rb"(?im)^From:[ \t]*(.*(?:\n[ \t].*)*)", raw)
        if m:
            cand = re.sub(r"\s+", " ",
                          m.group(1).decode("utf-8", "replace")).strip()
            if "�" not in cand:
                from_hdr = cand
    name, email = parseaddr(from_hdr)
    if "@" not in (email or ""):
        # Pipermail "at"-obfuscated From (e.g. "john.r.x at intel.com (Name)")
        # breaks parseaddr; recover the at-form so obfuscate can pseudonymise it
        # instead of leaving a bare local-part fragment ("john.r.x").
        m = re.search(r"[A-Za-z0-9._%+\-]+ at [A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                      from_hdr)
        email = m.group(0) if m else ""
    date_raw = msg.get("Date", "")
    date_iso: Optional[str] = None
    dt = None
    if date_raw:
        try:
            dt = parsedate_to_datetime(date_raw)
            # Normalise the sort key to UTC. date_iso is compared as a string
            # (ORDER BY), so mixed offsets would otherwise sort by local
            # wall-clock and put replies before the messages they answer.
            dt_utc = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            date_iso = dt_utc.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            dt = None
    if month is None:                       # month from the sender's local date
        month = dt.strftime("%Y-%B") if dt else "unknown"
    return {
        "month": month,
        # Message-Id / In-Reply-To are kept verbatim (outer-whitespace-stripped
        # only), NOT canonicalised. They are exact-match keys for dedup (the msgid
        # UNIQUE constraint) and for threading (in_reply_to == msgid), so in
        # principle a folded/whitespace/case variant of the same id could miss a
        # dedup or break a reply link. Deliberately left as-is: msgid is also the
        # permalink key and the obfuscation input, so normalising at STORE time
        # would renumber existing permalinks. Measured impact on the live corpus is
        # marginal -- 0 missed duplicates, and 16 of 33k reply links unrepaired,
        # all from one 2005 X.400 "@MHS" gateway whose ids were header-folded with
        # an embedded newline (case variants already collapse, since obfuscate.py
        # lowercases the address before hashing). If those links ever matter,
        # normalise only at COMPARISON time in threads.py (strip <>, collapse
        # internal whitespace), never the stored value.
        "msgid": (msg.get("Message-Id") or "").strip() or None,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "subject": normalize_subject(decode_mime(msg.get("Subject", ""))),
        "from_name": decode_mime(name),
        "from_email": email,
        "date_iso": date_iso,
        "date_raw": date_raw,
        "body": body_text(msg),
        "source": "list",
        "body_html": html_part(msg),   # sanitized HTML alternative, if any
        "raw": None,
    }


def gh_discussion_rows(disc: dict) -> list[dict]:
    """Flatten one GitHub Discussion (GraphQL node) into message rows.

    The opening post becomes the thread root; each comment and reply becomes
    a ``Re: <title>`` message linked via ``in_reply_to`` (node IDs), so the
    thread slots into the same schema as an email thread.
    """
    title = disc.get("title") or ""
    rows: list[dict] = []

    def row(node: dict, subject: str, parent: str | None) -> dict:
        login = ((node.get("author") or {}).get("login")) or "ghost"
        created = node.get("createdAt") or ""
        month = created[:4] + "-" + _MONTH_NAMES.get(created[5:7], "unknown") \
            if len(created) >= 7 else "unknown"
        return {
            "month": month,
            "msgid": node["id"],
            "in_reply_to": parent,
            "subject": subject,
            "from_name": login,
            "from_email": f"{login}@users.noreply.github.com",
            "date_iso": created or None,
            "date_raw": created,
            "body": node.get("body") or "",
            "source": "github",
            # GitHub server-renders bodyHTML, but run it through the same allowlist
            # sanitizer as e-mail HTML rather than trust a third party's markup
            # verbatim: body_to_html re-serves stored body_html without re-checking
            # it, so every source must sanitize at ingest (parity + defense-in-depth).
            "body_html": sanitize_html(node.get("bodyHTML") or "") or None,
            "raw": None,
        }

    rows.append(row(disc, title, None))
    re_subj = f"Re: {title}"
    for c in (disc.get("comments") or {}).get("nodes", []):
        parent = (c.get("replyTo") or {}).get("id") or disc["id"]
        rows.append(row(c, re_subj, parent))
        for r in (c.get("replies") or {}).get("nodes", []):
            rows.append(row(r, re_subj, (r.get("replyTo") or {}).get("id")
                                         or c["id"]))
    return rows


# An mbox "From " envelope line ends with an asctime "HH:MM:SS YYYY", and a
# real separator sits at the start of the file or after a blank line (the
# blank-line rule rejects forwarded "From ..." lines quoted inside a body).
# Matching this ourselves (rather than mailbox.mbox) also avoids its ASCII
# decode of the From_ line, which crashes on non-ASCII sender names and would
# silently drop an entire month.
_MBOX_FROM = re.compile(
    rb"(?m)(?:\A|(?<=\n\n))From .+\d{2}:\d{2}:\d{2} \d{4}\s*?$")


def iter_mbox(raw: bytes):
    """Split raw mbox bytes; yield ``(chunk, email.message.Message)``.

    THE one mbox splitter (Pipermail crawler + local-export import -- the
    import once carried its own copy WITHOUT the unescape, leaving stray
    ">From " in imported bodies). ``chunk`` is the full original entry
    (From_ line through the next separator), kept verbatim so a month's
    mbox can be regenerated for download; the parsed payload gets mboxrd
    unescaping (one ">" peeled off ">From " lines)."""
    starts = [m.start() for m in _MBOX_FROM.finditer(raw)]
    if not starts:
        return
    starts.append(len(raw))
    for i in range(len(starts) - 1):
        chunk = raw[starts[i]:starts[i + 1]]
        nl = chunk.find(b"\n")                       # drop the From_ envelope
        payload = chunk[nl + 1:] if nl != -1 else b""
        payload = re.sub(rb"(?m)^>(>*From )", rb"\1", payload)
        yield chunk, email.message_from_bytes(payload)


_MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

# This module owns the archive's "2024-January" month format, so the name
# table and its sort key live here (the inverse map is derived, so the two
# directions can never disagree). generate.py imports both.
MONTH_ORDER = {name: int(num) for num, name in _MONTH_NAMES.items()}


def month_key(month: str) -> tuple[int, int]:
    """Sortable (year, month#) for a '2024-January' label; (0, 0) if malformed."""
    try:
        year, name = month.split("-", 1)
        return (int(year), MONTH_ORDER.get(name, 0))
    except ValueError:
        return (0, 0)


_INSERT = """INSERT OR IGNORE INTO message
    (month, msgid, in_reply_to, subject, from_name, from_email,
     date_iso, date_raw, body, source, body_html, raw)
    VALUES (:month, :msgid, :in_reply_to, :subject, :from_name,
            :from_email, :date_iso, :date_raw, :body, :source, :body_html,
            :raw)"""


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert rows, skipping any whose msgid already exists. Returns # added."""
    before = conn.total_changes
    conn.executemany(_INSERT, rows)
    conn.commit()
    return conn.total_changes - before
