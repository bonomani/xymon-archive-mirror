#!/usr/bin/env python3
"""Independent, fail-closed privacy gate over the OBFUSCATED archive DB.

obfuscate.py (public) substitutes real e-mail addresses with pseudonyms. This
module does NOT trust that pass -- it re-reads the finished archive.db and
asserts the *output* invariant: every e-mail address that survived is on a safe
domain. Anything else aborts the build BEFORE archive.db.gz is written or
published, so an obfuscation regression fails closed instead of leaking PII.

Safe:
  * an address in _LIST_ALLOWLIST  -- the exact xymon/xymon-announce mailman
                                     addresses kept in the clear. Any OTHER
                                     @xymon.com address (e.g. a personal one)
                                     is NOT safe and fails closed, mirroring
                                     obfuscate's exact allowlist (not a whole
                                     -domain exemption). Keep the two in sync.
  * a domain ending in .invalid  -- RFC 2606 reserved, never routable, so it is
                                    never a real person's address. This is the
                                    pseudonym domain (user-<hash>@xymon.invalid).
  * a *pseudonym* (user-<12hex>@xymon.invalid) immediately followed by NON-dot
    junk -- e.g. user-..@xymon.invaliduser (two masked addresses glued in
    <mailto:>) or user-..@xymon.invalidXXX (a masked address glued to a redacted
    phone). The real address is already hashed away; only the reserved domain is
    smeared. A dot after .invalid is NOT allowed (that would be the *routable*
    xymon.invalid.example.com), so this exception cannot pass a real address.
  * an all-X domain (XXX.XXX.XXXX) -- redacted contact info, not an address.

Both address shapes are checked and BOTH can fail closed:
  * local@domain.tld           -- any non-safe domain is a leak.
  * local at domain.tld        -- a non-safe domain is a leak UNLESS the local
                                  part is a prose word obfuscate deliberately
                                  keeps (its _AT_STOP list, mirrored below, e.g.
                                  "available at sourceforge.net"). Anything
                                  obfuscate would have masked but didn't -> leak.
The TLD ends on (?![A-Za-z]) (not \\b) so a glued phone ("...sherwin.com216-…")
can't hide an address from the scanner.

Runs in TWO places, kept byte-identical (publish.sh warns on drift):
  * private vault (authoritative): rebuild.py gates what is PUBLISHED, before
    archive.db.gz is pushed to the public 'data' branch. Deliberately
    self-contained -- nothing imported from the obfuscate.py it audits.
  * public CI: pages.yml re-runs it over the archive.db.gz fetched from
    'data', gating what is DEPLOYED -- a bad or stale data push cannot ship
    cleartext addresses even if the publisher-side gate was skipped.

    python3 verify_obfuscation.py [archive.db]
"""
from __future__ import annotations

import email
import io
import re
import sqlite3
import sys
import tarfile
import zipfile
import zlib

# local@domain.tld  (g1=local g2=domain)  and  local at domain.tld  (same groups).
# local may be a quoted-string, domain may be a literal [ip] -- both reversible.
_ADDR = re.compile(
    r'((?:"[^"@\n]{1,64}"|[A-Za-z0-9._%+\-]+))'
    r'@([A-Za-z0-9.\-]+\.[A-Za-z]{2,}|\[[0-9A-Fa-f:.]{3,45}\])')
_AT = re.compile(
    r"\b([A-Za-z0-9._%+\-]+) at ([A-Za-z0-9.\-]+\.[A-Za-z]{2,})(?![A-Za-z])",
    re.I)
# scraper-dodging forms (mirror of obfuscate): "@" as %40 or (at)/[at]/{at};
# "." as (dot)/[dot]/{dot} or the word " dot ". A surviving one of these is a leak
# (obfuscate should have masked it); bare " at " keeps the prose stoplist.
_OBF_AT_HI = r"[\(\[\{]\s*at\s*[\)\]\}]|%40"
_OBF_DOTM = r"[\(\[\{]\s*dot\s*[\)\]\}]|\s+dot\s+|\."
_OBF = re.compile(
    rf"(?<![\w.%+\-])([A-Za-z0-9._+\-]+)\s*(?:({_OBF_AT_HI})|\s+at\s+)\s*"
    rf"((?:[A-Za-z0-9\-]+\s*(?:{_OBF_DOTM})\s*)+[A-Za-z]{{2,}})", re.I)
_OBF_HASDOT = re.compile(r"\bdot\b|[\(\[\{]\s*dot", re.I)   # word/bracket "dot"
# percent-encoded "@" (%40); no boundary guard (often inside encoded URLs).
_PCT = re.compile(r"[A-Za-z0-9._+\-]+%40[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)
# a quoted-printable soft line-break: "=" at end of line. Stripped before scanning
# so a QP-wrapped address is rejoined (xymon@xymon.co=\nm -> xymon@xymon.com).
_SOFTBREAK = re.compile(r"=\r?\n")


def _obf_canon(s: str) -> str:
    s = re.sub(_OBF_AT_HI, "@", s, flags=re.I)
    s = re.sub(r"\s+at\s+", "@", s, flags=re.I)
    s = re.sub(r"[\(\[\{]\s*dot\s*[\)\]\}]|\s+dot\s+", ".", s, flags=re.I)
    return re.sub(r"\s", "", s).lower()
# a pseudonym whose reserved domain is smeared by glued junk that adds NO further
# dotted label (a 2nd masked address, or a redacted phone): e.g.
# xymon.invaliduser / xymon.invalidXXX. Anchored with [^.]*$ so ANY further dot
# fails -- both the routable xymon.invalid.example.com and xymon.invaliduser.example.com.
_SAFE_PSEUDO = re.compile(r"user-[0-9a-f]{12}@xymon\.invalid[^.]*$", re.I)
# a pseudonym's reserved domain picked up as a " at "-local by following prose:
# "user-<hex>@xymon.invalid at cegeka.be" -> the " at " matcher sees local
# "xymon.invalid". Exempt ONLY when the exact user-<12hex>@ pseudonym precedes it
# (not e.g. victim.xymon.invalid@example.com, which is a real address).
_PSEUDO_TAIL = re.compile(r"user-[0-9a-f]{12}@$", re.I)
# an all-X "domain" is redacted contact info (a phone like 216.515.4000), not an
# address -- redact_contact ran after obfuscate and turned the digits into X.
_REDACTED = re.compile(r"[x.\-]+", re.I)
# exact @xymon.com addresses kept in the clear -- mirror of obfuscate.LIST_ALLOWLIST
# (the xymon / xymon-announce mailman addresses). Any other @xymon.com fails.
_LIST_NAMES = ("xymon", "xymon-announce")
_LIST_ROLES = ("", "-bounces", "-request", "-owner", "-join", "-leave",
               "-subscribe", "-unsubscribe", "-confirm")
_LIST_ALLOWLIST = frozenset(
    [f"{n}{r}@xymon.com" for n in _LIST_NAMES for r in _LIST_ROLES]
    + ["leave@xymon.com"])

# Prose locals obfuscate (public) intentionally does NOT convert in the
# "word at site.tld" form -- mirror of obfuscate._AT_STOP. A surviving " at "
# address whose local is NOT one of these is something obfuscate should have
# masked, so we fail closed on it. (Duplicated, not imported, to keep this gate
# independent of the public code it audits.)
_AT_STOP = frozenset((
    "look", "looking", "available", "unavailable", "hosted", "host", "hosting",
    "found", "find", "located", "locate", "running", "based", "documented",
    "download", "downloaded", "downloading", "pointed", "pointing", "aimed",
    "directed", "arrived", "arrive", "mirror", "mirrored", "archived", "posted",
    "online", "offline", "back", "out", "only", "even", "here", "there",
    "again", "stored", "kept", "released", "working", "started", "stopped",
    "registered", "listed", "linked", "published", "reachable", "accessible",
    "stay", "staying", "more", "once", "seen", "view", "viewed", "click",
    "clicking", "served", "serving",
))

# every published text/blob column obfuscate touches, per table. attachment
# filename/content_type/url are published (and filename/content_type rendered),
# so they are scanned too -- not just msgid/content.
_COLS = {
    "message": ("msgid", "in_reply_to", "from_name", "from_email",
                "subject", "body", "body_html", "raw"),
    "attachment": ("msgid", "filename", "content_type", "url", "content", "cid"),
}


def _safe(addr: str, dom: str) -> bool:
    d = dom.lower().rstrip(".")
    if addr.lower() in _LIST_ALLOWLIST:
        return True                       # exact allowlisted list/role address
    # RFC 2606 reserved (the pseudonym domain). The address regexes always
    # capture a dotted domain, so a bare "invalid" can't occur -- only .invalid.
    if d.endswith(".invalid"):
        return True
    if _REDACTED.fullmatch(d):
        return True                       # all-X redacted contact info
    return _SAFE_PSEUDO.match(addr.lower()) is not None   # pseudonym + non-dot junk


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("latin-1", "replace")
    return str(v)


_ARCH_MAX_DEPTH = 4
_ARCH_MAX_MEMBERS = 5000
_ARCH_MAX_BYTES = 200 * 1024 * 1024
_ARCH_MAX_RATIO = 1000


def _bounded_gunzip(data: bytes, limit: int):
    """Gunzip, aborting (None) once output would exceed `limit` -- a bomb can't be
    fully expanded into memory before the size guard fires."""
    if limit < 0:
        return None
    try:
        d = zlib.decompressobj(31)
        out = bytearray(d.decompress(data, limit + 1))
        while d.unconsumed_tail and len(out) <= limit:
            out += d.decompress(d.unconsumed_tail, limit + 1 - len(out))
        if len(out) > limit:
            return None
        out += d.flush()
        return None if len(out) > limit else bytes(out)
    except Exception:
        return None


# Binary image bodies generate false "addresses" out of pixel noise. A content
# blob is exempt from the byte-level scans ONLY after this gate INDEPENDENTLY
# proves it is a metadata-free PNG/JPEG (whitelist walk, nothing after the end
# marker) -- deliberately NOT by trusting the public stripper it audits. If
# that stripper ever regresses, images stop validating here, fall back to the
# text scan, hit binary noise and the gate fails loudly: still fail-closed.
_PNG_OK = {b"IHDR", b"PLTE", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
           b"sBIT", b"pHYs", b"bKGD", b"hIST", b"IDAT", b"IEND"}


def _clean_png(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    i = 8
    while i + 8 <= len(data):
        ln = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        if typ not in _PNG_OK:
            return False
        end = i + 12 + ln
        if end > len(data):
            return False
        if typ == b"IEND":
            return end == len(data)          # nothing may follow IEND
        i = end
    return False


def _clean_jpeg(data: bytes) -> bool:
    if not data.startswith(b"\xff\xd8"):
        return False
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return False
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:                   # scan data must end AT the EOI
            eoi = data.find(b"\xff\xd9", i)
            return eoi >= 0 and eoi + 2 == len(data)
        if marker == 0xD9:
            return False                     # EOI with no scan: not an image
        if 0xE1 <= marker <= 0xEF or marker == 0xFE:
            return False                     # metadata segment survived
        if marker == 0xE0 and data[i + 4:i + 9] != b"JFIF\x00":
            return False                     # APP0 that is not genuine JFIF
        ln = int.from_bytes(data[i + 2:i + 4], "big")
        if ln < 2 or i + 2 + ln > len(data):
            return False
        i += 2 + ln
    return False


def _verified_stripped_image(data: bytes) -> bool:
    return _clean_png(data) or _clean_jpeg(data)


def _is_container(content: bytes) -> bool:
    if content[:2] == b"\x1f\x8b" or content[:4] == b"PK\x03\x04":
        return True
    try:
        tarfile.open(fileobj=io.BytesIO(content)).close()
        return True
    except Exception:
        return False


def _inspect(content: bytes, depth: int = 0, budget=None):
    """Recursive read-only walk of a gz/zip/tar container, mirroring obfuscate.
    Returns the list of leaf member byte-strings, or None if it cannot be FULLY
    and SAFELY inspected (encrypted / read error / malformed / too deep / over a
    resource limit). None => the published container is NOT verifiable, which the
    gate treats as a failure (obfuscate should have withheld it). ZIP members are
    read by ZipInfo so duplicate filenames can't hide an entry."""
    if budget is None:
        budget = [_ARCH_MAX_MEMBERS, _ARCH_MAX_BYTES]
    if depth > _ARCH_MAX_DEPTH:
        return None
    if content[:2] == b"\x1f\x8b":
        inner = _bounded_gunzip(content, budget[1])
        if inner is None:
            return None
        budget[1] -= len(inner)
        return _inspect(inner, depth + 1, budget)
    if content[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(content))
            infos = z.infolist()
        except Exception:
            return None
        if any(i.flag_bits & 0x1 for i in infos):
            return None
        out = []
        for zi in infos:
            if zi.is_dir():
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            if zi.file_size > budget[1] or (
                    zi.compress_size and
                    zi.file_size / zi.compress_size > _ARCH_MAX_RATIO):
                return None
            try:
                data = z.read(zi)
            except Exception:
                return None
            budget[1] -= len(data)
            if budget[1] < 0:
                return None
            sub = _inspect(data, depth + 1, budget)
            if sub is None:
                if _is_container(data):
                    return None
                out.append(data)
            else:
                out.extend(sub)
        return out
    try:
        t = tarfile.open(fileobj=io.BytesIO(content))
    except Exception:
        return [content]
    out = []
    try:
        for mb in t.getmembers():
            if not mb.isfile():
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            if mb.size > budget[1]:
                return None
            f = t.extractfile(mb)
            if f is None:
                return None
            data = f.read()
            budget[1] -= len(data)
            if budget[1] < 0:
                return None
            sub = _inspect(data, depth + 1, budget)
            if sub is None:
                if _is_container(data):
                    return None
                out.append(data)
            else:
                out.extend(sub)
    except Exception:
        return None
    return out


def _mime_units(val):
    """Decode base64 / quoted-printable MIME leaf parts of a raw message and
    return (text_parts, uninspectable). A surviving address inside a transfer-
    encoded part (a base64 message body, an S/MIME signature, a QP body split
    across a soft line-break) is invisible to a plain text scan of `raw`, so we
    decode every encoded leaf and hand its bytes back to the address scanners.
    A container leaf (gz/zip/tar) is walked with _inspect, exactly as an
    attachment blob is; an un-walkable one sets the uninspectable flag (fail
    closed). Read-only; any parse error degrades to 'nothing extra to scan',
    which is safe because the plain text of `raw` is still scanned by the caller.
    Independent of obfuscate.py on purpose (this gate audits that code)."""
    if isinstance(val, (bytes, bytearray)):
        raw = bytes(val)
    elif isinstance(val, str):
        raw = val.encode("latin-1", "replace")
    else:
        return [], False
    if b"Content-Transfer-Encoding" not in raw:
        return [], False
    try:
        msg = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001
        return [], False
    parts, uninspectable = [], False
    for part in msg.walk():
        if part.is_multipart():
            continue
        cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte not in ("base64", "quoted-printable"):
            continue
        try:
            dec = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            continue
        if not dec:
            continue
        if _is_container(dec):
            members = _inspect(dec)
            if members is None:
                uninspectable = True
            else:
                parts += [_as_text(m) for m in members]
        else:
            parts.append(_as_text(dec))
    return parts, uninspectable


def scan(db: str):
    """Return (leaks, prose): lists of (table, col, rowid, match)."""
    conn = sqlite3.connect(db)
    leaks, prose = [], []
    try:
        for tbl, cols in _COLS.items():
            have = {c[1] for c in conn.execute(f"PRAGMA table_info({tbl})")}
            use = [c for c in cols if c in have]
            if not use:
                continue
            q = "SELECT id, " + ", ".join(use) + f" FROM {tbl}"
            for row in conn.execute(q):
                rid = row[0]
                for col, val in zip(use, row[1:]):
                    if (tbl == "attachment" and col == "content"
                            and isinstance(val, (bytes, bytearray))
                            and _verified_stripped_image(bytes(val))):
                        continue   # proven metadata-free pixels: binary noise,
                    #                no scannable text remains by construction
                    # the raw text, PLUS any text decompressed from an archive
                    # (so addresses inside a gz/zip/tar attachment are scanned).
                    parts = [_as_text(val)]
                    if isinstance(val, (bytes, bytearray)) and _is_container(bytes(val)):
                        members = _inspect(bytes(val))
                        if members is None:           # un-inspectable container published
                            leaks.append((tbl, col, rid, "<uninspectable archive>"))
                        else:
                            parts += [_as_text(mem) for mem in members]
                    # addresses hidden inside base64 / quoted-printable MIME parts
                    # (message bodies, S/MIME sigs) are invisible to a text scan of
                    # `raw` -- decode every transfer-encoded leaf and scan it too.
                    mparts, muninspectable = _mime_units(val)
                    parts += mparts
                    if muninspectable:
                        leaks.append((tbl, col, rid, "<uninspectable mime part>"))
                    for t in parts:
                        if not t:
                            continue
                        # Rejoin quoted-printable soft line-breaks (=\n / =\r\n)
                        # before scanning the raw bytes: QP wraps long lines mid-
                        # address, so an allowlisted address or a pseudonym can look
                        # like a truncated leak (xymon@xymon.co, user-..@xymon.inval)
                        # -- and, conversely, a REAL address split this way would
                        # hide from the regex. Rejoining reconstructs the logical
                        # text, killing the false positives and catching the splits.
                        t = _SOFTBREAK.sub("", t)
                        for m in _ADDR.finditer(t):
                            if not _safe(m.group(0), m.group(2)):
                                leaks.append((tbl, col, rid, m.group(0)))
                        for m in _AT.finditer(t):
                            local, dom = m.group(1), m.group(2)
                            canon = m.group(0).replace(" at ", "@", 1)
                            if _safe(canon, dom):     # real pseudonym / allowlist
                                continue
                            if local.lower() == "xymon.invalid" and \
                                    _PSEUDO_TAIL.search(t[:m.start()]):
                                continue              # pseudonym tail + prose
                            if local.lower() in _AT_STOP:
                                prose.append((tbl, col, rid, m.group(0)))   # warn
                            else:
                                leaks.append((tbl, col, rid, m.group(0)))   # fail
                        for m in _PCT.finditer(t):   # %40 (incl. inside URLs)
                            canon = re.sub("%40", "@", m.group(0), flags=re.I).lower()
                            if not _safe(canon, canon.partition("@")[2]):
                                leaks.append((tbl, col, rid, m.group(0)))
                        for m in _OBF.finditer(t):   # (at)/[at]/"dot" forms
                            hi = m.group(2)
                            if hi is None and not _OBF_HASDOT.search(m.group(3)):
                                continue              # plain " at " -> _AT loop owns it
                            canon = _obf_canon(m.group(0))
                            local, _, dom = canon.partition("@")
                            if _safe(canon, dom):
                                continue
                            if local == "xymon.invalid" and \
                                    _PSEUDO_TAIL.search(t[:m.start()]):
                                continue              # pseudonym tail + prose domain
                            if hi is None and local in _AT_STOP:
                                prose.append((tbl, col, rid, m.group(0)))
                            else:
                                leaks.append((tbl, col, rid, m.group(0)))
    finally:
        conn.close()
    return leaks, prose


def verify(db: str) -> None:
    """Scan the obfuscated DB and sys.exit on ANY real leak -- the gate is always
    fail-closed. The non-fatal "prose" matches are printed as a warning."""
    leaks, prose = scan(db)
    if prose:
        ex = "; ".join(sorted({p[3] for p in prose})[:5])
        print(f"privacy gate: {len(prose)} ' at '-form prose match(es) kept "
              f"(stoplist, non-fatal), e.g. {ex}", file=sys.stderr)
    if leaks:
        distinct = sorted({lk[3] for lk in leaks})
        sample = "\n  ".join(distinct[:20])
        more = "" if len(distinct) <= 20 else f"\n  ... (+{len(distinct) - 20} more)"
        sys.exit(f"!! privacy gate FAILED: {len(leaks)} cleartext address "
                 f"occurrence(s) ({len(distinct)} distinct) on non-safe domains "
                 f"survived obfuscation:\n  {sample}{more}")
    print("privacy gate: OK -- no cleartext non-safe addresses in the "
          "obfuscated DB")


if __name__ == "__main__":
    verify(sys.argv[1] if len(sys.argv) > 1 else "archive.db")
