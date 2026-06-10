#!/usr/bin/env python3
"""Replace personal email addresses with stable, irreversible pseudonyms.

`alice@example.com` -> `user-1a2b3c4d@xymon.invalid`, where the token is
`sha256(salt + address)`. Same address always maps to the same token (so
"messages from one person" still groups), but it cannot be reversed without
the salt. An exact allowlist of `@xymon.com` list/role addresses (LIST_ALLOWLIST)
and already-pseudonymised `@xymon.invalid` addresses are left untouched, so the
pass is idempotent. Other `@xymon.com` addresses (e.g. personal ones) ARE
pseudonymised -- the whole domain is not exempt.

Applies to from_email, subject, body, raw (mbox export) and text attachments,
i.e. everything that ends up published in archive.db.gz and the static site.

Salt resolution: $OBFUSCATE_SALT, else private/salt.txt, else a weak built-in
default (with a warning) so it never fails open and leaks cleartext.

    python3 obfuscate.py [archive.db]
"""
from __future__ import annotations

import base64
import email
import gzip
import hashlib
import io
import json
import os
import quopri
import tarfile
import zipfile
from collections import Counter
import re
import sqlite3
import sys
from pathlib import Path

import mailstore                       # decode_payload + apply_fast_pragmas
import webfetch                        # gunzip_bounded (shared zip-bomb guard)

_DEFAULT = "xymon-archive-public-fallback-salt"   # weak; set a real one
LIST_DOMAIN = "xymon.com"             # public list/infra address -> kept as-is
PSEUDO_DOMAIN = "xymon.invalid"       # real addresses map to user-<h>@<this>
# canonical e-mail-address shape, one source of truth for text + bytes scans.
# Local may be a quoted-string ("john doe"@x); domain may be a literal ([1.2.3.4]
# / [IPv6:..]) -- both are valid, reversible addresses that must be masked too.
_ADDR = (r'(?:"[^"@\n]{1,64}"|[A-Za-z0-9._%+\-]+)'
         r'@(?:[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|\[[0-9A-Fa-f:.]{3,45}\])')
_T = re.compile(_ADDR)
_B = re.compile(_ADDR.encode())
_KEEP = (f"@{LIST_DOMAIN}", f"@{PSEUDO_DOMAIN}")
# a bare pseudonym at the START of a matched address; used to repair a pseudonym
# the greedy address regex over-captured (user-<h>@xymon.invalid<junk>).
_PSEUDO_AT = re.compile(rf"user-[0-9a-f]{{12}}@{re.escape(PSEUDO_DOMAIN)}")
_PSEUDO_AT_B = re.compile(_PSEUDO_AT.pattern.encode())
# Exact list / role addresses kept in the clear: the mailman addresses of the
# xymon and xymon-announce lists. Everything else @xymon.com -- including a
# maintainer's personal address -- is pseudonymised like any other address. This
# is an EXACT allowlist, not a whole-domain exemption. Mirrored in the private
# verify_obfuscation.py (_LIST_ALLOWLIST); keep the two in sync.
_LIST_NAMES = ("xymon", "xymon-announce")
_LIST_ROLES = ("", "-bounces", "-request", "-owner", "-join", "-leave",
               "-subscribe", "-unsubscribe", "-confirm")
LIST_ALLOWLIST = frozenset(
    [f"{n}{r}@{LIST_DOMAIN}" for n in _LIST_NAMES for r in _LIST_ROLES]
    + [f"leave@{LIST_DOMAIN}"])
LIST_ALLOWLIST_B = frozenset(a.encode() for a in LIST_ALLOWLIST)

# Pipermail "at"-obfuscated addresses (user@host -> "user at host"). These are
# overwhelmingly real addresses, so we convert any "local at domain.tld" --
# except when the local part is a plain English word that only precedes a site
# name in prose ("available at sourceforge.net"). That stoplist holds words
# that are never email local parts (role names like info/support/security are
# NOT listed, so they get pseudonymised).
# The TLD ends on "not a letter" rather than a word boundary \b: list footers
# sometimes glue a phone straight onto the domain ("...sherwin.com216-515-4000"),
# and a digit after a letter is NOT a \b, which made the whole address fail to
# match and leak. (?![A-Za-z]) still terminates the TLD on a digit / hyphen.
_AT = re.compile(
    r"\b[A-Za-z0-9._%+\-]+ at [A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z])", re.I)
_AT_B = re.compile(
    rb"\b[A-Za-z0-9._%+\-]+ at [A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z])", re.I)
_AT_STOP = frozenset((
    "look", "looking", "available", "unavailable", "hosted", "host", "hosting",
    "found", "find", "located", "locate", "running", "based", "documented",
    "download", "downloaded", "downloading", "pointed", "pointing", "aimed",
    "directed", "arrived", "arrive", "mirror", "mirrored", "archived", "posted",
    "online", "offline", "back", "out", "only", "even", "here", "there",
    "again", "stored", "kept", "released", "working", "started", "stopped",
    "registered", "listed", "linked", "published", "reachable", "accessible",
    "stay", "staying", "released", "released", "more", "once", "seen", "view",
    "viewed", "click", "clicking", "look", "running", "served", "serving",
))
_AT_FULL = re.compile(
    r"^\s*[A-Za-z0-9._%+\-]+ at [A-Za-z0-9.\-]+\.[A-Za-z]{2,}\s*$", re.I)
# Inside <...> or (...) it's unambiguously an address, so any local part goes
# (catches quoted headers like <henrik at hswn.dk>).
_AT_BR = re.compile(
    r"(?<=[<(])[A-Za-z0-9._%+\-]+ at [A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?=[>)])",
    re.I)
_AT_BR_B = re.compile(
    rb"(?<=[<(])[A-Za-z0-9._%+\-]+ at [A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?=[>)])",
    re.I)
# Footer lines sometimes lose their whitespace ("...xymon.comhttp://.../listinfo"),
# which makes the address regex run its TLD into the "http", swallowing the URL
# into the pseudonym. Restore the boundary before a glued URL scheme first.
_GLUE = re.compile(r"(?<=[\w.@])(?=https?://)")
_GLUE_B = re.compile(rb"(?<=[\w.@])(?=https?://)")

# Deliberate scraper-dodging address forms that are still reversible and MUST be
# masked: "@" written as %40 or (at)/[at]/{at}; "." written as (dot)/[dot]/{dot}
# or the word " dot ". High-confidence markers (brackets/%40) are masked
# unconditionally; the ambiguous bare word " at " keeps the _AT_STOP prose guard.
# Whitespace (incl. a wrapped newline) is allowed around the markers.
_OBF_AT_HI = r"[\(\[\{]\s*at\s*[\)\]\}]|%40"
_OBF_DOTM = r"[\(\[\{]\s*dot\s*[\)\]\}]|\s+dot\s+|\."
_OBF_LOCAL = r"[A-Za-z0-9._+\-]+"        # no % (so %40 reads as the @ marker)
_OBF_DOM = rf"(?:[A-Za-z0-9\-]+\s*(?:{_OBF_DOTM})\s*)+[A-Za-z]{{2,}}"
_OBF = re.compile(
    rf"(?<![\w.%+\-])({_OBF_LOCAL})\s*(?:({_OBF_AT_HI})|\s+at\s+)\s*({_OBF_DOM})",
    re.I)
_OBF_B = re.compile(_OBF.pattern.encode(), re.I)
_OBF_HASDOT = re.compile(r"\bdot\b|[\(\[\{]\s*dot", re.I)   # word/bracket "dot"
# percent-encoded "@" (%40). Unlike the word/bracket forms this needs NO boundary
# guard -- %40 is never prose, and these often sit inside percent-encoded URLs
# (e.g. Outlook SafeLinks "...%7Ctschmidt%40micron.com%7C..."), so a lookbehind
# would wrongly skip them. Whatever the captured local, masking removes the
# recoverable address.
_PCT = re.compile(r"[A-Za-z0-9._+\-]+%40[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)
_PCT_B = re.compile(_PCT.pattern.encode(), re.I)


def _obf_canon(s: str) -> str:
    """An _OBF match -> canonical 'local@domain' (markers normalised, lowercased)."""
    s = re.sub(_OBF_AT_HI, "@", s, flags=re.I)
    s = re.sub(r"\s+at\s+", "@", s, flags=re.I)
    s = re.sub(r"[\(\[\{]\s*dot\s*[\)\]\}]|\s+dot\s+", ".", s, flags=re.I)
    return re.sub(r"\s", "", s).lower()


def get_salt() -> bytes:
    s = os.environ.get("OBFUSCATE_SALT")
    if s:
        return s.encode()
    p = Path(__file__).with_name("private") / "salt.txt"
    if p.exists():
        return p.read_text().strip().encode()
    print("!! obfuscate: no OBFUSCATE_SALT / private/salt.txt -- using weak "
          "default salt", file=sys.stderr)
    return _DEFAULT.encode()


def _pseudo(addr_lower: str, salt: bytes) -> str:
    # 12 hex (48 bits) keeps Message-Id collisions negligible (msgid is UNIQUE)
    h = hashlib.sha256(salt + addr_lower.encode()).hexdigest()[:12]
    return f"user-{h}@{PSEUDO_DOMAIN}"


def make_repl(salt: bytes):
    # Keep list addresses, and anything already pseudonymised. "Already
    # pseudonymised" means an EXACT user-<12hex>@xymon.invalid match (via
    # _PSEUDO_AT) -- NOT merely a string containing "xymon.invalid", which would
    # exempt a real address like victim.xymon.invalid@example.com. When the greedy
    # regex grabs trailing chars after a pseudonym (user-<h>@xymon.invalid.cvf,
    # ...@xymon.invaliduser) _PSEUDO_AT still matches the prefix, so we REPAIR it:
    # collapse back to the bare user-<hash>@xymon.invalid, dropping the suffix.
    def repl_t(m: "re.Match[str]") -> str:
        al = m.group(0).lower()
        if al in LIST_ALLOWLIST:
            return m.group(0)
        norm = _PSEUDO_AT.match(al)
        if norm:
            return al[:norm.end()]            # bare pseudonym, suffix dropped
        return _pseudo(al, salt)

    def repl_b(m: "re.Match[bytes]") -> bytes:
        al = m.group(0).lower()
        if al in LIST_ALLOWLIST_B:
            return m.group(0)
        norm = _PSEUDO_AT_B.match(al)
        if norm:
            return al[:norm.end()]
        return _pseudo(al.decode("ascii", "replace"), salt).encode()

    def at_t(m: "re.Match[str]") -> str:           # "user at host" -> pseudonym
        addr = m.group(0).lower().replace(" at ", "@", 1)
        if _PSEUDO_AT.match(addr) or addr in LIST_ALLOWLIST:
            return m.group(0)
        if addr.split("@", 1)[0] in _AT_STOP:      # prose, not an address
            return m.group(0)
        return _pseudo(addr, salt)

    def at_b(m: "re.Match[bytes]") -> bytes:
        addr = m.group(0).lower().replace(b" at ", b"@", 1)
        if _PSEUDO_AT_B.match(addr) or addr in LIST_ALLOWLIST_B:
            return m.group(0)
        if addr.split(b"@", 1)[0].decode("ascii", "replace") in _AT_STOP:
            return m.group(0)
        return _pseudo(addr.decode("ascii", "replace"), salt).encode()

    def at_br(m) -> str:           # inside <...>: always an address (no stoplist)
        addr = m.group(0).lower().replace(" at ", "@", 1)
        if _PSEUDO_AT.match(addr) or addr in LIST_ALLOWLIST:
            return m.group(0)
        return _pseudo(addr, salt)

    def at_br_b(m) -> bytes:
        addr = m.group(0).lower().replace(b" at ", b"@", 1)
        if _PSEUDO_AT_B.match(addr) or addr in LIST_ALLOWLIST_B:
            return m.group(0)
        return _pseudo(addr.decode("ascii", "replace"), salt).encode()

    def _obf_mask(canon, hi_present, dom_str) -> bool:
        # A bare " at " with a literal-dot domain is the classic pipermail form --
        # leave it to the _AT handler (prose stoplist + pseudonym-tail guard); _OBF
        # OWNS only the marker forms and the word/bracket "dot" forms.
        if not hi_present and not _OBF_HASDOT.search(dom_str):
            return False                           # defer to _AT (leave unchanged)
        if not hi_present and canon.split("@", 1)[0] in _AT_STOP:
            return False                           # prose word -> keep
        if canon in LIST_ALLOWLIST or _PSEUDO_AT.match(canon):
            return False
        return True                                # mask

    def pct_repl(m):                               # local%40domain (incl. in URLs)
        canon = re.sub("%40", "@", m.group(0), flags=re.I).lower()
        if canon in LIST_ALLOWLIST or _PSEUDO_AT.match(canon):
            return m.group(0)
        return _pseudo(canon, salt)

    def pct_repl_b(m):
        canon = re.sub(b"%40", b"@", m.group(0), flags=re.I).decode(
            "ascii", "replace").lower()
        if canon in LIST_ALLOWLIST or _PSEUDO_AT.match(canon):
            return m.group(0)
        return _pseudo(canon, salt).encode()

    def obf_repl(m):                               # (at)/[at]/%40/"dot" forms
        canon = _obf_canon(m.group(0))
        if _obf_mask(canon, m.group(2) is not None, m.group(3)):
            return _pseudo(canon, salt)
        return m.group(0)

    def obf_repl_b(m):
        canon = _obf_canon(m.group(0).decode("ascii", "replace"))
        dom = m.group(3).decode("ascii", "replace")
        if _obf_mask(canon, m.group(2) is not None, dom):
            return _pseudo(canon, salt).encode()
        return m.group(0)

    def text(s):                                   # @, "at" and obfuscated forms
        if not s:
            return s
        s = _GLUE.sub(" ", s)                      # un-glue a run-together URL
        s = _PCT.sub(pct_repl, s)                  # %40 (incl. inside URLs)
        s = _OBF.sub(obf_repl, s)                  # (at)/[at]/dot -> pseudonym
        return _AT.sub(at_t, _AT_BR.sub(at_br, _T.sub(repl_t, s)))

    def name(s):                                   # from_name: also whole plain at-addr
        if s and _AT_FULL.match(s):
            return _pseudo(s.strip().lower().replace(" at ", "@", 1), salt)
        return text(s)

    def blob(b):                                   # @, "at" and obfuscated forms
        if not b:
            return b
        b = _GLUE_B.sub(b" ", b)                   # un-glue a run-together URL
        b = _PCT_B.sub(pct_repl_b, b)
        b = _OBF_B.sub(obf_repl_b, b)
        return _AT_B.sub(at_b, _AT_BR_B.sub(at_br_b, _B.sub(repl_b, b)))

    return repl_t, repl_b, text, name, blob


_US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC").split()
_STREET = (r"St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|"
           r"Ct|Court|Pl|Place|Ter|Terrace|Cir|Circle|Pkwy|Parkway|Hwy|"
           r"Highway|Sq|Square")
# A phone number is the anchor: NANP 3-3-4 with separators, optional +CC. It is
# distinctive enough to redact anywhere; the street/suite/ZIP patterns (which
# overlap prose like "#1" or "... in place") are only redacted when they sit
# near a phone -- i.e. inside the same signature block.
_RX_PHONE = re.compile(
    r"(?<![\dX])(?:\+?\d{1,2}[ .\-])?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?![\dX])")
# International / unformatted phones are caught by their label instead (a colon
# is required, so "Office 2010" is not a phone). The number after the label may
# group digits any way (spaces, "/", "-"); we redact it if it holds >=4 digits.
_PHONE_LABELS = (r"tel|telephone|tel[eé]fono|telefono|telf|tlf|phone|ph|fax|"
                 r"mobile|mobil|m[oó]vil|movil|cell|cellular|office|oficina")
_RX_LABELED = re.compile(
    r"(?i)\b(?:" + _PHONE_LABELS + r")\b\.?[ \t]*:[ \t]*"
    r"([+(]?\d[\d \t().+/\-]{3,})")
_RX_EXT = re.compile(r"(?i)\b(?:ext|extension)\b\.?[ \t]*:?[ \t]*(\d{2,6})\b")
_RX_NEARPHONE = [
    re.compile(r"\b\d{1,6}(?:\s+[A-Za-z0-9.#'\-]+){1,4}\s+(?:" + _STREET +
               r")\b\.?", re.I),                              # street address
    re.compile(r"\b(?:Suite|Ste|Apt|Apartment|Unit|Bldg|Building|Floor|"
               r"Room|Rm|Mailstop|MS)\.?\s*#?\s*\d+\b", re.I),  # suite/unit
    re.compile(r"\b(?:" + "|".join(_US_STATES) + r")\s+\d{5}(?:-\d{4})?\b"),  # ZIP
]
_DIGIT = re.compile(r"\d")


def redact_contact(s):
    """Replace the digits inside personal contact details with X. Phone numbers
    anywhere; street/suite/ZIP only within ~300 chars of a phone (a signature).
    Digit->X is length-preserving, so phone spans stay valid after redaction."""
    if not s:
        return s
    is_bytes = isinstance(s, (bytes, bytearray))
    t = s.decode("latin-1") if is_bytes else s
    spans = [m.span() for m in _RX_PHONE.finditer(t)]
    t = _RX_PHONE.sub(lambda m: _DIGIT.sub("X", m.group(0)), t)

    def _red_grp(m):                          # redact digits in group 1 only
        g = m.group(1)
        if sum(ch.isdigit() for ch in g) < 4:
            return m.group(0)
        off = m.start(1) - m.start(0)
        return m.group(0)[:off] + _DIGIT.sub("X", g)
    for m in _RX_LABELED.finditer(t):
        if sum(ch.isdigit() for ch in m.group(1)) >= 4:
            spans.append(m.span(1))
    t = _RX_LABELED.sub(_red_grp, t)
    for m in _RX_EXT.finditer(t):
        spans.append(m.span(1))
    t = _RX_EXT.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)]
                    + _DIGIT.sub("X", m.group(1)), t)
    if spans:
        def near(p):
            return any(a - 300 <= p <= b + 300 for a, b in spans)
        for rx in _RX_NEARPHONE:
            t = rx.sub(lambda m: _DIGIT.sub("X", m.group(0))
                       if near(m.start()) else m.group(0), t)
    return t.encode("latin-1") if is_bytes else t


def _needs_attachment_redaction(ct: str, content: bytes) -> bool:
    """Redact addresses in an attachment by MIME type, or for ANY type whose
    bytes embed an address -- list mail mislabels text files (.c/.sh/.obj) as
    application/octet-stream, and even binary dumps can carry real addresses
    that would otherwise leak verbatim to the public site."""
    if not content:
        return False
    if (ct or "").lower().startswith(("text/", "application/x", "message/")):
        return True
    return bool(_B.search(content))   # any type embedding an address


# A compressed attachment (gz/zip/tar) hides addresses from the byte-level
# scanners -- blob()/_B see only the compressed stream. So decompress it,
# recursively, and inspect the members. We do not rebuild archives (re-deflate
# risks corruption or, worse, a partial redaction that still leaks); instead an
# archive that carries a real address is WITHHELD from publication (the original
# stays in the private attachments vault).
_WITHHELD = b"[attachment withheld: contained personal data not safe to publish]"


# archive-bomb guards: cap depth, member count, total expanded size, and the
# per-member compression ratio. Exceeding any makes the container
# "uninspectable" -> withheld. Sizes are checked BEFORE materialising a member
# (streaming gunzip, ZipInfo.file_size, TarInfo.size) so a bomb cannot exhaust
# memory before the guard fires.
_ARCH_MAX_DEPTH = 4
_ARCH_MAX_MEMBERS = 5000
_ARCH_MAX_BYTES = 200 * 1024 * 1024
_ARCH_MAX_RATIO = 1000


def _bounded_gunzip(data: bytes, limit: int):
    """Gunzip but abort (return None) as soon as output would exceed `limit`.

    The algorithm is webfetch.gunzip_bounded -- the ONE shared zip-bomb guard
    -- adapted to this pipeline's contract: an over-limit or corrupt archive
    is "unsafe, skip it" (None), never a crash mid-publish."""
    if limit < 0:
        return None
    try:
        return webfetch.gunzip_bounded(data, limit)
    except Exception:  # noqa: BLE001
        return None


# --- image metadata stripping --------------------------------------------------
# Images bypass the text scrubbers entirely, but their metadata containers
# (EXIF, XMP, comments, textual chunks) can carry author names, GPS positions
# and software fingerprints. Publish pixels only: stdlib walkers rebuild the
# file without metadata segments. Anything we cannot parse -- or any image
# format we have no walker for -- is withheld, same stance as archives that
# cannot be fully inspected (the original stays in the private vault).

_IMG_EXTS = (".png", ".jpg", ".jpeg")


def _is_image(ct: str, name: str) -> bool:
    return ((ct or "").lower().startswith("image/")
            or (name or "").lower().endswith(_IMG_EXTS))


def _strip_png_meta(data: bytes):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    drop = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}
    out, i = bytearray(data[:8]), 8
    while i + 8 <= len(data):
        ln = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        end = i + 12 + ln                       # len + type + data + crc
        if end > len(data):
            return None                         # truncated/malformed
        if typ not in drop:                     # untouched chunks keep their CRC
            out += data[i:end]
        i = end
        if typ == b"IEND":
            return bytes(out)
    return None


def _strip_jpeg_meta(data: bytes):
    if not data.startswith(b"\xff\xd8"):
        return None
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                      # fill byte
            i += 1
            continue
        if marker in (0xD9, 0xDA):              # EOI / SOS: copy scan to end
            out += data[i:]
            return bytes(out)
        ln = int.from_bytes(data[i + 2:i + 4], "big")
        if ln < 2 or i + 2 + ln > len(data):
            return None
        # APP1..APP15 (EXIF/XMP/ICC-adjacent metadata) and COM comments are
        # dropped; APP0 (JFIF) and the structural segments stay.
        if not (0xE1 <= marker <= 0xEF or marker == 0xFE):
            out += data[i:i + 2 + ln]
        i += 2 + ln
    return None


def strip_image_metadata(ct: str, name: str, data: bytes):
    """Cleaned bytes for png/jpeg, or None (unparseable / unsupported image
    format) -- the caller withholds on None."""
    low = (name or "").lower()
    c = (ct or "").lower()
    if c == "image/png" or low.endswith(".png"):
        return _strip_png_meta(data)
    if c == "image/jpeg" or low.endswith((".jpg", ".jpeg")):
        return _strip_jpeg_meta(data)
    return None


def _is_container(content: bytes) -> bool:
    if content[:2] == b"\x1f\x8b" or content[:4] == b"PK\x03\x04":
        return True
    try:
        tarfile.open(fileobj=io.BytesIO(content)).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _inspect(content: bytes, depth: int = 0, budget=None):
    """Read-only recursive walk of a gz/zip/tar container. Returns the list of
    leaf member byte-strings, or None if it cannot be FULLY and SAFELY inspected
    -- encrypted, read error, malformed, nested too deep, or over a resource
    limit. None ALWAYS means 'withhold'; it is never treated as 'clean'. ZIP
    members are read by ZipInfo so duplicate filenames can't hide an entry."""
    if budget is None:
        budget = [_ARCH_MAX_MEMBERS, _ARCH_MAX_BYTES]   # [members_left, bytes_left]
    if depth > _ARCH_MAX_DEPTH:
        return None
    if content[:2] == b"\x1f\x8b":                       # gzip
        inner = _bounded_gunzip(content, budget[1])      # capped BEFORE materialise
        if inner is None:
            return None
        budget[1] -= len(inner)
        return _inspect(inner, depth + 1, budget)
    if content[:4] == b"PK\x03\x04":                     # zip
        try:
            z = zipfile.ZipFile(io.BytesIO(content))
            infos = z.infolist()
        except Exception:  # noqa: BLE001
            return None
        if any(i.flag_bits & 0x1 for i in infos):        # encrypted
            return None
        out = []
        for zi in infos:
            if zi.is_dir():
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            # check declared sizes BEFORE reading (bomb guard)
            if zi.file_size > budget[1] or (
                    zi.compress_size and
                    zi.file_size / zi.compress_size > _ARCH_MAX_RATIO):
                return None
            try:
                data = z.read(zi)                        # by ZipInfo (dup-safe)
            except Exception:  # noqa: BLE001
                return None
            budget[1] -= len(data)
            if budget[1] < 0:
                return None
            sub = _inspect(data, depth + 1, budget)
            if sub is None:
                if _is_container(data):
                    return None                          # nested, unwalkable
                out.append(data)
            else:
                out.extend(sub)
        return out
    try:                                                 # tar (any compression)
        t = tarfile.open(fileobj=io.BytesIO(content))
    except Exception:  # noqa: BLE001
        return [content]                                 # not a container -> leaf
    out = []
    try:
        for mb in t.getmembers():
            if not mb.isfile():
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            if mb.size > budget[1]:                       # declared size guard
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
    except Exception:  # noqa: BLE001
        return None
    return out


def _member_unsafe(b: bytes) -> bool:
    """True if these bytes carry an address that is not an already-masked
    pseudonym (.invalid) nor an allowlisted list address."""
    for m in _B.finditer(b):
        al = m.group(0).lower()
        dom = al.split(b"@", 1)[-1]
        if dom == b"xymon.invalid" or dom.endswith(b".invalid"):
            continue
        if al in LIST_ALLOWLIST_B:
            continue
        return True
    return False


def _is_text_member(b: bytes) -> bool:
    """A member we can safely obfuscate in place: text / source / scripts /
    config. NUL bytes or a low printable ratio => treat as opaque binary."""
    if b"\x00" in b:
        return False
    if not b:
        return True
    printable = sum(1 for c in b if c in (9, 10, 13) or 32 <= c <= 126 or c >= 160)
    return printable / len(b) > 0.85


def _clean_archive(data: bytes, blob, depth: int = 0, budget=None):
    """Rebuild a gz/zip/tar archive with addresses scrubbed, DETERMINISTICALLY
    (fixed mtimes/owners/order) so identical input -> identical bytes. Returns
    (bytes, ok); ok=False means it cannot be safely sanitised -- a binary member
    carrying an address, an encrypted / malformed archive, or one that trips the
    depth / member / size guards -- and the caller withholds it. ZIP members are
    read by ZipInfo so duplicate filenames can't hide an entry."""
    if budget is None:
        budget = [_ARCH_MAX_MEMBERS, _ARCH_MAX_BYTES]
    if depth > _ARCH_MAX_DEPTH:
        return data, False
    if data[:2] == b"\x1f\x8b":                          # gzip (incl. tar.gz)
        inner = _bounded_gunzip(data, budget[1])         # capped BEFORE materialise
        if inner is None:
            return data, False
        budget[1] -= len(inner)
        red, ok = _clean_archive(inner, blob, depth + 1, budget)
        if not ok:
            return data, False
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
            g.write(red)
        return buf.getvalue(), True
    if data[:4] == b"PK\x03\x04":                        # zip (incl. docx/xlsx)
        try:
            src = zipfile.ZipFile(io.BytesIO(data))
            infos = src.infolist()
        except Exception:  # noqa: BLE001
            return data, False
        if any(i.flag_bits & 0x1 for i in infos):        # encrypted -> withhold
            return data, False
        out = io.BytesIO()
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for i in sorted(infos, key=lambda x: x.filename):
                    if i.is_dir():
                        continue
                    budget[0] -= 1
                    if budget[0] < 0:
                        return data, False
                    if i.file_size > budget[1] or (
                            i.compress_size and
                            i.file_size / i.compress_size > _ARCH_MAX_RATIO):
                        return data, False               # bomb guard (pre-read)
                    member = src.read(i)                 # by ZipInfo (dup-safe)
                    budget[1] -= len(member)
                    if budget[1] < 0:
                        return data, False
                    red, ok = _clean_archive(member, blob, depth + 1, budget)
                    if not ok:
                        return data, False
                    z.writestr(zipfile.ZipInfo(i.filename), red)   # fixed 1980 ts
        except Exception:  # noqa: BLE001
            return data, False
        return out.getvalue(), True
    try:                                                 # tar (any compression)
        src = tarfile.open(fileobj=io.BytesIO(data))
    except Exception:  # noqa: BLE001
        src = None
    if src is not None:
        out = io.BytesIO()
        try:
            with tarfile.open(fileobj=out, mode="w") as t:
                for mb in sorted(src.getmembers(), key=lambda m: m.name):
                    if not mb.isfile():
                        continue
                    budget[0] -= 1
                    if budget[0] < 0:
                        return data, False
                    if mb.size > budget[1]:               # declared size guard
                        return data, False
                    member = src.extractfile(mb).read()
                    budget[1] -= len(member)
                    if budget[1] < 0:
                        return data, False
                    red, ok = _clean_archive(member, blob, depth + 1, budget)
                    if not ok:
                        return data, False
                    mb.size = len(red)
                    mb.mtime = 0
                    mb.uid = mb.gid = 0
                    mb.uname = mb.gname = ""
                    t.addfile(mb, io.BytesIO(red))
        except Exception:  # noqa: BLE001
            return data, False
        return out.getvalue(), True
    # leaf (not an archive)
    if not _member_unsafe(data):
        return data, True                  # no address -> keep verbatim
    if _is_text_member(data):
        return redact_contact(blob(data)), True   # text -> pseudonymise + redact phones
    return data, False                     # binary carrying an address -> withhold


# A base64- or quoted-printable-encoded MIME part hides addresses from the
# byte-level scanners in blob(): base64 has no '@'/' at '/%40 in its alphabet, and
# a QP soft line-break (=\n) can split an address across two lines so the regex
# never matches it. The published `raw` column (and the downloadable per-month
# mbox built from it) therefore leaked real addresses buried in base64 message
# bodies and S/MIME signatures. We walk the MIME tree, decode each transfer-
# encoded leaf, scrub it with the SAME blob()+redact_contact / _clean_archive used
# everywhere else, and splice the re-encoded bytes back IN PLACE.
_TENC_WITHHELD = b"[encoded part withheld: contained personal data not safe to publish]"


def _reencode_part(decoded: bytes, cte: str, sample: bytes) -> bytes:
    """Re-encode `decoded` in its original transfer encoding, matching the sample
    region's trailing-newline and CRLF/LF convention so the splice stays a clean
    drop-in. Only ever called for a part that actually changed."""
    out = base64.encodebytes(decoded) if cte == "base64" \
        else quopri.encodestring(decoded)
    if not sample.endswith(b"\n") and out.endswith(b"\n"):
        out = out[:-1]
    if b"\r\n" in sample and b"\r\n" not in out:
        out = out.replace(b"\n", b"\r\n")
    return out


def _scrub_transfer_encoded(raw, blob):
    """Mask addresses hidden inside base64 / quoted-printable MIME parts of a raw
    message. Surgical and deterministic: only the encoded-body regions that
    actually changed are rewritten, so a message with nothing to mask is returned
    BYTE-IDENTICAL (no spurious republish). A container part (gz/zip/tar) is run
    through _clean_archive (sanitise-or-withhold) like an attachment; everything
    else is flat-scrubbed. Best-effort by design: a part that cannot be located
    for splicing is left for the independent privacy gate to catch -- fail closed,
    never fail open."""
    if not raw or not isinstance(raw, (bytes, bytearray)) \
            or b"Content-Transfer-Encoding" not in raw:
        return raw
    raw = bytes(raw)
    try:
        msg = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001  unparseable -> leave to blob()/the gate
        return raw
    cursor, edits = 0, []
    for part in msg.walk():
        if part.is_multipart():
            continue
        cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte not in ("base64", "quoted-printable"):
            continue
        payload = part.get_payload(decode=False)
        if not isinstance(payload, str) or not payload.strip():
            continue
        try:
            decoded = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            continue
        if not decoded:
            continue
        if _is_container(decoded):
            cleaned, ok = _clean_archive(decoded, blob)
            new = cleaned if ok else _TENC_WITHHELD
        else:
            new = redact_contact(blob(decoded))
        if new == decoded:
            continue                       # nothing masked -> leave bytes untouched
        # locate the exact original encoded body in raw (surrogateescape round-trips
        # the bytes the parser read); advance a cursor so identical payloads in
        # later parts still resolve in order.
        enc = payload.encode("ascii", "surrogateescape")
        pos = raw.find(enc, cursor)
        if pos < 0:
            pos = raw.find(enc)
        if pos < 0:
            continue                       # cannot place it -> gate will fail closed
        cursor = pos + len(enc)
        edits.append((pos, pos + len(enc), _reencode_part(new, cte, enc)))
    if not edits:
        return raw
    edits.sort()
    out, last = bytearray(), 0
    for s, e, repl in edits:
        out += raw[last:s] + repl
        last = e
    out += raw[last:]
    return bytes(out)


def obfuscate(db: str) -> None:
    salt = get_salt()
    repl_t, repl_b, text, name, blob = make_repl(salt)

    # curated email -> real display name (name-overrides.json in CWD, i.e. the
    # private vault at rebuild time). Applied on the REAL address before it is
    # pseudonymised, so the chosen name is baked into the obfuscated DB. Absent
    # in the public CI (different CWD), where rows are already done anyway.
    overrides = {}
    _opath = os.path.join(os.getcwd(), "name-overrides.json")
    if os.path.exists(_opath):
        try:
            overrides = {k.strip().lower().replace(" at ", "@", 1): v
                         for k, v in json.load(open(_opath, encoding="utf-8")).items()
                         if not k.startswith("_")}
            print(f"name overrides: {len(overrides)} entries")
        except Exception:
            overrides = {}

    conn = sqlite3.connect(db)
    mailstore.apply_fast_pragmas(conn)   # throwaway build DB -> skip fsync/journal

    # Incremental marker: rows already obfuscated on a prior pass carry
    # obfuscated=1 and are skipped, so a re-run over an already-obfuscated DB
    # (notably the public CI rebuild) does no regex work. New rows -- a fresh
    # rebuild from sources, or a REFRESH crawl -- default to 0 and get done.
    for tbl in ("message", "attachment"):
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({tbl})")]
        if "obfuscated" not in cols:
            conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN obfuscated INTEGER DEFAULT 0")

    # Backfill map: a sender who used a real display name on SOME messages but
    # a bare address on others should show that real name everywhere -- not a
    # local-part derivation. Built from the real (pre-mask) From of the rows
    # about to be processed; prefer a multi-word name, then the most frequent.
    _seen = {}
    for fe2, fn2 in conn.execute(
            "SELECT from_email, from_name FROM message "
            "WHERE COALESCE(obfuscated, 0)=0"):
        fn2s = (fn2 or "").strip()
        if not fe2 or not fn2s:
            continue
        if re.match(r"^[^@\s]+@[^@\s]+$",
                    fn2s.lower().replace(" at ", "@", 1)):
            continue                           # the "name" is itself an address
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", fn2s):
            continue                           # the "name" is an IP address
        _seen.setdefault(fe2.strip().lower().replace(" at ", "@", 1),
                         Counter())[fn2s] += 1
    realnames = {fe2: max(c, key=lambda nm: (len(nm.split()) >= 2, c[nm]))
                 for fe2, c in _seen.items()}

    changed = 0
    # msgid/in_reply_to are obfuscated too (old Sendmail-style IDs embed real
    # addresses). Same deterministic mapping keeps threading consistent: an
    # In-Reply-To resolves to its parent's identically-obfuscated Message-Id.
    rows = conn.execute(
        "SELECT id, msgid, in_reply_to, from_name, from_email, subject, "
        "body, body_html, raw FROM message WHERE COALESCE(obfuscated, 0)=0"
    ).fetchall()
    for mid, msgid, irt, fn, fe, subj, body, bhtml, raw in rows:
        ofn = fn          # original name; override/derive below may reassign fn,
                          # so the change-test must compare against THIS, else an
                          # override on a kept @xymon.com address is silently lost
        nmsgid = _T.sub(repl_t, msgid) if msgid else msgid
        nirt = _T.sub(repl_t, irt) if irt else irt
        # no real display name (empty, or the name IS the address) -> derive a
        # readable one from the real address local part, before it is masked.
        if fe:
            # Pipermail scrubs '@' to ' at ' in BOTH name and address; normalise
            # before comparing / extracting the local part.
            fe_n = fe.strip().lower().replace(" at ", "@", 1)
            if fe_n in overrides:                  # curated name wins
                fn = overrides[fe_n]
            else:
                fn_s = (fn or "").strip()
                fn_n = fn_s.lower().replace(" at ", "@", 1)
                # derive when there is no real name: empty, the "name" is itself
                # an address, or it is an IP address (a misconfigured client put
                # its local IP in the From display name).
                if (not fn_n or re.match(r"^[^@\s]+@[^@\s]+$", fn_n)
                        or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", fn_s)):
                    if fe_n in realnames:          # same address: real name wins
                        fn = realnames[fe_n]
                    else:
                        local = fe_n.split("@", 1)[0]
                        toks = [p for p in re.split(r"[._+-]+", local) if p]
                        if len(toks) > 1 and toks[-1].lower() in (
                                "ext", "external", "contractor"):
                            toks = toks[:-1]       # drop contractor marker
                        derived = " ".join(toks).title()
                        if derived:
                            fn = derived
        nfn = name(fn)               # display name may BE an "at"-address
        nfe = text(fe)
        nsubj = text(subj)
        nbody = redact_contact(text(body))
        nbhtml = redact_contact(text(bhtml))
        # blob() masks cleartext in headers and unencoded parts; _scrub_transfer
        # _encoded then reaches addresses buried in base64 / QP MIME parts that the
        # byte-level pass cannot see (base64 has no '@'; QP can split an address
        # across a soft line-break). base64 regions are inert to blob() -- their
        # alphabet has no '@', ' at ', or '%' -- so running blob() first is safe.
        nraw = _scrub_transfer_encoded(redact_contact(blob(raw)), blob)
        if (nmsgid, nirt, nfn, nfe, nsubj, nbody, nbhtml, nraw) != \
                (msgid, irt, ofn, fe, subj, body, bhtml, raw):
            conn.execute(
                "UPDATE message SET msgid=?, in_reply_to=?, from_name=?, "
                "from_email=?, subject=?, body=?, body_html=?, raw=? "
                "WHERE id=?",
                (nmsgid, nirt, nfn, nfe, nsubj, nbody, nbhtml, nraw, mid))
            changed += 1
    conn.executemany("UPDATE message SET obfuscated=1 WHERE id=?",
                     [(r[0],) for r in rows])

    # attachments link to their message by msgid -> obfuscate identically;
    # text/* and application/x* payloads may carry addresses too. filename is
    # rendered on the site and url ships in the published DB, so scrub addresses
    # out of those metadata fields as well (url is not the rendered link -- the
    # site serves the stored content -- so rewriting it breaks nothing).
    att_changed = 0
    arows = conn.execute(
        "SELECT id, msgid, content_type, content, filename, url FROM attachment "
        "WHERE COALESCE(obfuscated, 0)=0").fetchall()
    for aid, amsgid, ct, content, fname, url in arows:
        if amsgid:
            nam = _T.sub(repl_t, amsgid)
            if nam != amsgid:
                conn.execute("UPDATE attachment SET msgid=? WHERE id=?",
                             (nam, aid))
        nfn, nurl = text(fname), text(url)
        if nfn != fname or nurl != url:
            conn.execute("UPDATE attachment SET filename=?, url=? WHERE id=?",
                         (nfn, nurl, aid))
        if content and _is_image(ct, fname):
            # pixels only: strip metadata containers; unparseable -> withhold
            # (note: content is used as-is -- the byte-level address scrubber
            # must never touch a binary image body).
            cleaned = strip_image_metadata(ct, fname, bytes(content))
            if cleaned is None:
                conn.execute(
                    "UPDATE attachment SET content=?, size=?, "
                    "content_type='text/plain' WHERE id=?",
                    (_WITHHELD, len(_WITHHELD), aid))
            elif cleaned != content:
                conn.execute(
                    "UPDATE attachment SET content=?, size=? WHERE id=?",
                    (cleaned, len(cleaned), aid))
            att_changed += 1
        elif content and _is_container(content):
            # compressed: byte-scanners can't see inside. Fully walk it first.
            # members is None => can't fully + safely inspect (encrypted, read
            # error, malformed, too deep, resource limit) -> WITHHOLD. If a member
            # carries an address, SANITISE (rebuild with text members scrubbed)
            # and publish only if a second full scan is clean; else WITHHOLD. The
            # original is always preserved in the private vault.
            members = _inspect(content)
            if members is None:
                withhold = True
            elif any(_member_unsafe(m) for m in members):
                cleaned, ok = _clean_archive(content, blob)
                rescan = _inspect(cleaned) if ok else None
                if ok and rescan is not None and not any(
                        _member_unsafe(m) for m in rescan):
                    conn.execute(
                        "UPDATE attachment SET content=?, size=? WHERE id=?",
                        (cleaned, len(cleaned), aid))
                    withhold = False
                else:
                    withhold = True
            else:
                withhold = False                  # fully inspected, clean
            if withhold:
                conn.execute(
                    "UPDATE attachment SET content=?, size=?, "
                    "content_type='text/plain' WHERE id=?",
                    (_WITHHELD, len(_WITHHELD), aid))
            att_changed += 1
        elif _needs_attachment_redaction(ct, content):
            new = redact_contact(blob(content))   # pseudonyms + phone redaction
            if new != content:
                conn.execute(
                    "UPDATE attachment SET content=?, size=? WHERE id=?",
                    (new, len(new), aid))
                att_changed += 1
    conn.executemany("UPDATE attachment SET obfuscated=1 WHERE id=?",
                     [(a[0],) for a in arows])

    conn.commit()
    if rows or arows:        # row rewrites leave free pages; compact the file
        conn.execute("VACUUM")
    conn.close()
    print(f"obfuscated: {changed} messages, {att_changed} text attachments "
          f"({len(rows)} scanned)")


if __name__ == "__main__":
    obfuscate(sys.argv[1] if len(sys.argv) > 1 else "archive.db")
