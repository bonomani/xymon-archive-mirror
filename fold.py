"""Server-side quote folding for thread pages: parent-anchored, content-proven.

A reply on this list usually carries a full copy of the message it answers
(top-posting). The archive *has* that earlier text, so instead of guessing
quotes from markers or client-specific markup, each message is matched against
everything said earlier in its own thread:

  1. lines whose words are mostly covered by earlier-message 6-word shingles
     are quote candidates;
  2. the EARLIEST candidate that starts a validated tail (>=85% of all words
     from there to the end covered by earlier text) is the cut point;
  3. the cut sweeps upward over "furniture" lines (Outlook From:/Von: header
     fields, "X wrote:" attributions, mail-gateway banners, separators) that
     belong to the quote but are not themselves quoted text;
  4. everything from the swept cut to the end is wrapped in ONE
     <details class=q> whose summary names the quoted author (provenance =
     whichever earlier message contributed the matched text -- no reliance on
     In-Reply-To, which is missing/mangled in decades-old mail).

Validation is the safety: a low-coverage tail (an inline replier's answers, an
off-list forward, pasted headers) gets NO fold here -- the client script's
conservative per-run dedup remains as the fallback layer. The failure
direction is always under-folding; new text can never be hidden by this pass.

Operates on the RENDERED body HTML (the exact text a reader sees), so plain
and HTML mail take the same path and offsets cannot drift from the page.
"""
from __future__ import annotations

import re

from lxml import html as lhtml

K = 6                     # word-shingle size (matches the client script)
_MIN_LINE_WORDS = 4       # a shorter line can't prove it is quoted by itself
_LINE_COVER = 0.8         # fraction of a line's words covered -> quote candidate
_TAIL_COVER = 0.85        # fraction of cut..end words covered -> fold validated
_MIN_TAIL_COVERED = 12    # too little duplicated text is not worth a fold
_MIN_VISIBLE_WORDS = 3    # never fold a message down to less than this
_MAX_CANDIDATES = 20      # cut candidates tried before giving up
_MAX_SWEEP = 12           # non-blank furniture lines the cut may climb over
_MAX_SWEEP_TOTAL = 60     # hard cap including blanks (runaway guard)

_WORD = re.compile(r"\S+")
_NORM = re.compile(r"[^a-z0-9]+")

# Furniture between the author's text and the quoted copy: header fields
# (From:/Von:/Gesendet:/Betreff: ...), attributions ("On ... wrote:"),
# separators, gateway banners (they carry the sender's address or a URL),
# and bare quote markers. Only consulted in the bounded sweep zone directly
# above a VALIDATED quote tail, so breadth here cannot hide a free-standing
# reply.
_FURNITURE = re.compile(
    r"^\s*$"
    r"|^\s*>"
    r"|^\s*[\wÀ-ÿ-]{1,15}\s*:\s"                      # Field: value
    r"|^\s*(On|Le|El|Am|Op|Den|P[aå]|Il giorno)\b"    # attribution opener
    r"|(wrote|schrieb|escribi[oó]|escreveu|skrev|ha scritto|a [eé]crit)\s*:?\s*$"
    r"|^\s*[-_*=]{2,}\s*(\S.*)?$"                     # separator / Original Message
    r"|@"                                             # any line carrying an address
    r"|https?://",                                    # any line carrying a URL
    re.I)

_BLOCKS = frozenset(
    "p div li blockquote tr pre h1 h2 h3 h4 h5 h6 ul ol table details".split())


def _norm_words(text):
    """Normalized words -- the matching currency. Punctuation and quote
    markers vanish, so '>'-wrapped and reflowed copies still align."""
    out = []
    for m in _WORD.finditer(text or ""):
        w = _NORM.sub("", m.group(0).lower())
        if w:
            out.append(w)
    return out


# --- rendered-HTML text model -------------------------------------------------
#
# The fold must cut the DOM exactly where the text analysis decided, so both
# views come from one walk: a flat list of text SEGMENTS (an lxml node's
# .text or .tail) with global offsets, plus '\n' breaks at <br> and around
# block elements -- the same logical-line model as the client script.

class _Seg:
    __slots__ = ("el", "attr", "text", "start")

    def __init__(self, el, attr, text, start):
        self.el, self.attr, self.text, self.start = el, attr, text, start


def _walk(body):
    """Return (full_text, [segments]). Breaks are plain '\n' in full_text with
    no backing segment, so segment offsets stay aligned with full_text.
    Outside <pre>, a raw newline in a text node is mere whitespace in HTML --
    it goes into full_text as a space (same length, offsets unchanged) so a
    soft-wrapped paragraph stays ONE logical line; inside <pre> it is a real
    line break. Segments keep the original text for the DOM surgery."""
    segs, parts = [], []
    pos = 0

    def emit(el, attr, t, in_pre):
        nonlocal pos
        if t:
            segs.append(_Seg(el, attr, t, pos))
            parts.append(t if in_pre else t.replace("\n", " "))
            pos += len(t)

    def brk():
        nonlocal pos
        parts.append("\n")
        pos += 1

    def rec(el, in_pre):
        tag = el.tag if isinstance(el.tag, str) else ""
        in_pre = in_pre or tag == "pre"
        if tag == "br" or tag in _BLOCKS:
            brk()
        emit(el, "text", el.text, in_pre)
        for c in el:
            rec(c, in_pre)
            emit(c, "tail", c.tail, in_pre)
        if tag in _BLOCKS:
            brk()

    root_pre = body.tag == "pre"
    emit(body, "text", body.text, root_pre)
    for c in body:
        rec(c, root_pre)
        emit(c, "tail", c.tail, root_pre)
    return "".join(parts), segs


def _lines(full):
    """[(start, end, text)] logical lines of the joined text."""
    out, start = [], 0
    while True:
        i = full.find("\n", start)
        if i < 0:
            out.append((start, len(full), full[start:]))
            return out
        out.append((start, i, full[start:i]))
        start = i + 1


# --- the cut decision -----------------------------------------------------------

def _plan(full, prior):
    """Decide the fold for one message: (cut_offset, src_index) or None.
    ``prior`` maps shingle -> index of the earliest message that said it."""
    lines = _lines(full)
    toks = []                                   # (norm_word, line_index)
    li = 0
    for m in _WORD.finditer(full):
        w = _NORM.sub("", m.group(0).lower())
        if not w:
            continue
        while li < len(lines) - 1 and m.start() > lines[li][1]:
            li += 1
        toks.append((w, li))
    ws = [t[0] for t in toks]

    cov = [None] * len(ws)                      # source message per covered word
    for i in range(len(ws) - K + 1):
        hit = prior.get(" ".join(ws[i:i + K]))
        if hit is not None:
            for j in range(i, i + K):
                if cov[j] is None:
                    cov[j] = hit

    nw = [0] * len(lines)                       # words / covered words per line
    nc = [0] * len(lines)
    for i, (_, l) in enumerate(toks):
        nw[l] += 1
        if cov[i] is not None:
            nc[l] += 1

    candidates = [i for i in range(len(lines))
                  if nw[i] >= _MIN_LINE_WORDS and nc[i] / nw[i] >= _LINE_COVER]
    for ci in candidates[:_MAX_CANDIDATES]:
        tail = [i for i, t in enumerate(toks) if t[1] >= ci]
        covered = sum(1 for i in tail if cov[i] is not None)
        if covered < _MIN_TAIL_COVERED or covered / len(tail) < _TAIL_COVER:
            continue
        cut_line = ci                           # climb over quote furniture
        swept = 0                               # blanks are free; real lines capped
        for _ in range(_MAX_SWEEP_TOTAL):
            if cut_line == 0 or swept >= _MAX_SWEEP:
                break
            above = lines[cut_line - 1][2]
            if not _FURNITURE.search(above):
                break
            if above.strip():
                swept += 1
            cut_line -= 1
        if sum(nw[i] for i in range(cut_line)) < _MIN_VISIBLE_WORDS:
            return None                          # would hollow the message out
        src = next((cov[i] for i in tail if cov[i] is not None), None)
        return lines[cut_line][0], src
    return None


# --- DOM surgery ------------------------------------------------------------------

def _split_seg(seg, off):
    """Split a segment's text at ``off``; the tail becomes a fresh <span>
    placed immediately after the head, and is returned as the fold's first
    node. (A <span> is legal inside <pre>, <p> and <blockquote> alike.)"""
    head, tail = seg.text[:off], seg.text[off:]
    span = lhtml.Element("span")
    span.text = tail
    if seg.attr == "text":
        seg.el.text = head
        seg.el.insert(0, span)
    else:
        seg.el.tail = head
        parent = seg.el.getparent()
        parent.insert(parent.index(seg.el) + 1, span)
    return span


def _fold_from(body, segs, cut, who):
    """Wrap everything in ``body`` from text offset ``cut`` to the end in a
    <details class=q>. Ancestors are split at the cut so formatting elements
    (<pre>, <blockquote>, ...) survive on both sides."""
    seg = next((s for s in segs if s.start + len(s.text) > cut), None)
    if seg is None:
        return False
    if cut > seg.start:
        node = _split_seg(seg, cut - seg.start)
    elif seg.attr == "text":
        node = seg.el                            # fold the whole element
    else:
        node = _split_seg(seg, 0)                # tail text -> own node

    # climb to a direct child of body, splitting each ancestor: `cur` and its
    # following siblings move into a shallow copy of the ancestor placed right
    # after it; the copy becomes the unit to carry up to the next level.
    cur = node
    while True:
        parent = cur.getparent()
        if parent is None:
            return False
        if parent is body:
            break
        wrapper = lhtml.Element(parent.tag, dict(parent.attrib))
        for sib in list(parent[parent.index(cur):]):
            wrapper.append(sib)                  # moves them (incl. cur)
        if not (parent.text or "").strip() and len(parent) == 0:
            gp = parent.getparent()
            gp.insert(gp.index(parent), wrapper)
            wrapper.tail, parent.tail = parent.tail, None
            gp.remove(parent)                    # ancestor emptied -> replace
        else:
            gp = parent.getparent()
            gp.insert(gp.index(parent) + 1, wrapper)
            wrapper.tail, parent.tail = parent.tail, None
        cur = wrapper

    det = lhtml.Element("details")
    det.set("class", "q")
    summary = lhtml.Element("summary")
    arrow = lhtml.Element("span")
    arrow.set("class", "ar")
    arrow.text = "▸"
    summary.append(arrow)
    if who:
        label = lhtml.Element("span")
        label.set("class", "meta")
        label.text = f" quoted from {who}"
        summary.append(label)
    det.append(summary)

    i = body.index(cur)
    movers = list(body[i:])
    body.insert(i, det)
    for sib in movers:
        det.append(sib)
    return True


# --- public entry -------------------------------------------------------------------

def fold_thread(bodies, authors):
    """``bodies``: rendered body-HTML strings of one thread, chronological.
    ``authors``: display name per message (provenance labels). Returns the
    list with each validated quote tail wrapped in <details class=q>; a
    message with no provable tail is returned unchanged."""
    out = []
    prior: dict = {}
    for mi, raw in enumerate(bodies):
        folded = raw
        full = None
        try:
            root = lhtml.fragment_fromstring(raw or "<div></div>",
                                             create_parent="x-fold")
            body = root[0] if (len(root) == 1
                               and not (root.text or "").strip()) else root
            full, segs = _walk(body)
            if prior:
                plan = _plan(full, prior)
                if plan is not None:
                    cut, src = plan
                    who = (authors[src]
                           if src is not None and 0 <= src < mi else None)
                    if _fold_from(body, segs, cut, who):
                        ser = lhtml.tostring(root, encoding="unicode")
                        folded = re.sub(r"^<x-fold>|</x-fold>$", "", ser)
        except Exception:
            folded = raw                          # any surprise -> render as-is
            full = None
        out.append(folded)
        ws = _norm_words(full) if full is not None else _norm_words(
            re.sub(r"<[^>]+>", " ", raw or ""))
        for j in range(len(ws) - K + 1):
            prior.setdefault(" ".join(ws[j:j + K]), mi)
    return out
