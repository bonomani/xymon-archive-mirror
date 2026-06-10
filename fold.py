"""Server-side quote folding for thread pages: parent-anchored, content-proven.

A reply on this list usually carries a full copy of the message it answers
(top-posting). The archive *has* that earlier text, so instead of guessing
quotes from markers or client-specific markup, each message is matched against
everything said earlier in its own thread:

  1. words covered by earlier-message 6-word shingles are quote evidence;
  2. a foldable line must have EVERY normalized word covered, or be an
     explicitly recognized quote-furniture line (Outlook headers, an
     attribution, a separator, or a known external-mail banner);
  3. maximal safe runs with enough duplicated text become independent folds,
     so an inline answer splits the quote instead of being hidden inside it;
  4. each <details class=q> names the earlier author who contributed most of
     its matched text -- no reliance on In-Reply-To, which is missing/mangled
     in decades-old mail.

Validation is deliberately one-sided: any uncovered prose token keeps its
whole logical line visible. This costs some folds when a mailer has genuinely
edited a quote, but a one-word answer or correction can never be treated as
acceptable "noise" inside a long duplicate.

Operates on the RENDERED body HTML (the exact text a reader sees), so plain
and HTML mail take the same path and offsets cannot drift from the page.
"""
from __future__ import annotations

import re
import sys

# lxml is the build's only third-party runtime dependency, and only this
# feature needs it: without it the build still renders everything, just with
# unfolded thread pages (the client script's folder remains). That keeps
# build.sh runnable on a bare stdlib Python and survives a stale workflow
# copy on the 'data' branch.
try:
    from lxml import html as lhtml
except ImportError:                                   # pragma: no cover
    lhtml = None
    print("fold: lxml not installed -- thread pages render UNFOLDED "
          "(pip install lxml)", file=sys.stderr)

K = 6                     # word-shingle size (matches the client script)
_MIN_LINE_WORDS = 4       # a shorter line can't prove it is quoted by itself
_MIN_TAIL_COVERED = 12    # too little duplicated text is not worth a fold
_MIN_VISIBLE_WORDS = 3    # never fold a message down to less than this

_WORD = re.compile(r"\S+")
# C0 control characters (minus \t \n \r): illegal in XML, so lxml refuses to
# re-serialize text containing them -- a NUL pasted into a 2003 mail made the
# whole message silently render unfolded. Replace each with a SPACE before
# parsing: same string length (offsets unchanged) and words stay separated
# (plain stripping glued "foo\x0cbar" into one token and broke the shingle
# match against the clean copy in the parent message).
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Quote furniture is intentionally an allowlist. Generic ``Key: value``
# lines, addresses and URLs are prose: "Warning: do not deploy" and "See
# https://..." must stay visible even immediately above a proven quote.
_HEADER = re.compile(
    r"^\s*(From|Von|De|Da|Van|Fra|Från"
    r"|Sent|Gesendet|Date|Envoy[eé]e?|Enviad[oa]|Inviat[oa]"
    r"|Verzonden|Sendt|Skickat"
    r"|To|An|Aan|Til|À|Cc|Bcc"
    r"|Subject|Betreff|Objet|Asunto|Oggetto|Onderwerp|Assunto|Emne|Ämne"
    r"|Reply-To)\s*:\s+\S", re.I)
_ATTRIBUTION = re.compile(
    r"^\s*(On|Le|El|Am|Op|Den|P[aå]|Il giorno)\b.*"
    r"(wrote|schrieb|escribi[oó]|escreveu|skrev|ha scritto|a [eé]crit)\s*:?\s*$",
    re.I)
_SEPARATOR = re.compile(
    r"^\s*[-_*=]{2,}\s*(Original Message|Ursprüngliche Nachricht"
    r"|Message d'origine|Mensaje original|Messaggio originale"
    r"|Oorspronkelijk bericht|Oprindelig meddelelse"
    r"|Ursprungligt meddelande|Opprinnelig melding)?\s*[-_*=]*\s*$",
    re.I)
_GATEWAY = re.compile(
    r"^\s*(WARNUNG:\s*Diese E-?Mail kam von außerhalb der Organisation"
    r"|CAUTION:\s*This (email|message) originated from outside"
    r"|WARNING:\s*This (email|message) (came|originated) from outside"
    r"|\[?EXTERNAL (EMAIL|MESSAGE)\]?)\b", re.I)
_BARE_QUOTE = re.compile(r"^\s*>+\s*$")

_BLOCKS = frozenset(
    "p div li blockquote tr pre h1 h2 h3 h4 h5 h6 ul ol table details".split())

# observable health: generate.py prints this after a render so a regression
# (messages silently falling back to unfolded) shows up in the build log.
STATS = {"errors": 0}


def _norm_words(text):
    """Normalized words -- the matching currency. Punctuation and quote
    markers vanish, so '>'-wrapped and reflowed copies still align."""
    out = []
    for m in _WORD.finditer(text or ""):
        w = _norm_word(m.group(0))
        if w:
            out.append(w)
    return out


def _norm_word(raw):
    """Unicode-aware alphanumeric token normalization."""
    return "".join(c for c in raw.casefold() if c.isalnum())


def _is_furniture(text):
    return bool(_ATTRIBUTION.search(text)
                or _SEPARATOR.search(text) or _GATEWAY.search(text)
                or _BARE_QUOTE.search(text))


def _header_furniture(lines):
    """Mark only real multi-field mail-header blocks, not lone prose such as
    ``Subject: use the staging configuration``."""
    marks = [False] * len(lines)
    matches = [_HEADER.search(text) for _, _, text in lines]
    i = 0
    while i < len(lines):
        if matches[i] is None:
            i += 1
            continue
        start = i
        while (i + 1 < len(lines)
               and (matches[i + 1] is not None
                    or not lines[i + 1][2].strip())):
            i += 1
        names = [matches[j].group(1).casefold() for j in range(start, i + 1)
                 if matches[j] is not None]
        if len(names) >= 2 and any(
                name in {"from", "von", "de", "da", "van", "fra", "från"}
                for name in names):
            for j in range(start, i + 1):
                marks[j] = True
        i += 1
    return marks


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
    """Return non-overlapping ``(cut, end|None, src_index)`` fold plans.

    ``prior`` maps each shingle to the latest earlier message that said it.
    A line enters a plan only when every normalized word is covered, or the
    line is explicit quote furniture. Uncovered prose therefore splits runs
    and remains visible, even when it is only one word or one changed token
    inside an otherwise copied paragraph.
    """
    lines = _lines(full)
    toks = []                                   # (norm_word, line_index)
    li = 0
    for m in _WORD.finditer(full):
        w = _norm_word(m.group(0))
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

    blank = [not text.strip() for _, _, text in lines]
    header_furn = _header_furniture(lines)
    furn = [header_furn[i] or _is_furniture(text)
            for i, (_, _, text) in enumerate(lines)]
    exact = [nw[i] > 0 and nc[i] == nw[i] for i in range(len(lines))]
    safe = [blank[i] or furn[i] or exact[i] for i in range(len(lines))]

    planned = []
    i = 0
    while i < len(lines):
        if not safe[i]:
            i += 1
            continue
        start = i
        while i + 1 < len(lines) and safe[i + 1]:
            i += 1
        stop = i

        evidence = [j for j in range(start, stop + 1) if exact[j]]
        covered = sum(nc[j] for j in range(start, stop + 1))
        if evidence and covered >= _MIN_TAIL_COVERED:
            first, last = evidence[0], evidence[-1]
            # Keep only quote-adjacent blank/furniture/covered lines. This
            # trims parser-generated empty lines at the message boundaries.
            cut_line = first
            while cut_line > start and safe[cut_line - 1]:
                cut_line -= 1
            end_line = last
            while end_line < stop and safe[end_line + 1]:
                end_line += 1

            covered_lines = sum(1 for j in range(cut_line, end_line + 1)
                                if exact[j] and nw[j])
            led = any(furn[j] for j in range(cut_line, first))
            trailing = all(blank[j] for j in range(end_line + 1, len(lines)))
            strong_line = any(nw[j] >= _MIN_LINE_WORDS
                              for j in range(cut_line, end_line + 1)
                              if exact[j])
            if ((covered_lines >= 2 or led or trailing)
                    and (strong_line or covered_lines >= 2)):
                sources = {}
                for ti, (_, line_index) in enumerate(toks):
                    if cut_line <= line_index <= end_line and cov[ti] is not None:
                        sources[cov[ti]] = sources.get(cov[ti], 0) + 1
                src = max(sources, key=sources.get) if sources else None
                planned.append((cut_line, end_line, src))
        i += 1

    hidden_lines = {j for start, stop, _ in planned
                    for j in range(start, stop + 1)}
    if sum(nw[j] for j in range(len(lines)) if j not in hidden_lines) \
            < _MIN_VISIBLE_WORDS:
        return []
    return [
        (lines[start][0],
         None if all(blank[j] for j in range(stop + 1, len(lines)))
         else lines[stop][1],
         src)
        for start, stop, src in planned
    ]


# --- DOM surgery ------------------------------------------------------------------

def _split_seg(seg, off):
    """Split a segment's text at ``off``; the tail becomes a fresh <span>
    placed immediately after the head, and is returned as the new node. The
    segment's snapshot shrinks to the head so a LATER split of the same
    segment (a bounded fold's start after its end was materialized) cannot
    resurrect text that already moved out. (A <span> is legal inside <pre>,
    <p> and <blockquote> alike.)"""
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
    seg.text = head
    return span


def _materialize(body, segs, off):
    """Split the DOM at text offset ``off`` and climb to a direct child of
    ``body``: every ancestor is split so that the returned node and all its
    following body-level siblings hold exactly the content from ``off`` on.
    Returns None when ``off`` is at/after the end (nothing to split)."""
    seg = next((s for s in segs if s.start + len(s.text) > off), None)
    if seg is None:
        return None
    if off > seg.start:
        node = _split_seg(seg, off - seg.start)
    elif seg.attr == "text" and seg.el is not body:
        node = seg.el                            # boundary at element start
    else:
        node = _split_seg(seg, 0)                # tail/root text -> own node

    cur = node
    while True:
        parent = cur.getparent()
        if parent is None:
            return None
        if parent is body:
            return cur
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


def _fold_range(body, segs, cut, end, who):
    """Wrap ``body``'s content from text offset ``cut`` to ``end`` (None =
    the end of the message) in a <details class=q>. The END boundary is
    materialized first -- splitting there cannot disturb offsets before it;
    the start split then works on the already-shrunk segment snapshots."""
    stop = _materialize(body, segs, end) if end is not None else None
    cur = _materialize(body, segs, cut)
    if cur is None:
        return False

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
    movers = []
    for sib in list(body[i:]):
        if stop is not None and sib is stop:
            break
        movers.append(sib)
    body.insert(i, det)
    for sib in movers:
        det.append(sib)
    return True


# --- public entry -------------------------------------------------------------------

def fold_thread(bodies, authors):
    """``bodies``: rendered body-HTML strings of one thread, chronological.
    ``authors``: display name per message (provenance labels). Returns the
    list with each validated exact quote run wrapped in <details class=q>; a
    message with no provable run is returned unchanged."""
    if lhtml is None:                          # no lxml -> no server-side folds
        return list(bodies)
    out = []
    prior: dict = {}
    for mi, raw in enumerate(bodies):
        folded = raw
        full = None
        try:
            cleaned = _CTRL.sub(" ", raw) if raw else raw
            root = lhtml.fragment_fromstring(cleaned or "<div></div>",
                                             create_parent="x-fold")
            body = root[0] if (len(root) == 1
                               and not (root.text or "").strip()) else root
            full, segs = _walk(body)
            if prior:
                plans = _plan(full, prior)
                for cut, end, src in reversed(plans):
                    who = (authors[src]
                           if src is not None and 0 <= src < mi else None)
                    _fold_range(body, segs, cut, end, who)
                if plans:
                    ser = lhtml.tostring(root, encoding="unicode")
                    folded = re.sub(r"^<x-fold>|</x-fold>$", "", ser)
        except Exception as exc:                  # any surprise -> render as-is,
            folded = raw                          # but never silently: an audit
            full = None                           # found 11 messages lost here.
            STATS["errors"] += 1
            print(f"fold: message {mi} ({authors[mi] if mi < len(authors) else '?'}) "
                  f"rendered unfolded: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        out.append(folded)
        ws = _norm_words(full) if full is not None else _norm_words(
            re.sub(r"<[^>]+>", " ", raw or ""))
        for j in range(len(ws) - K + 1):
            prior[" ".join(ws[j:j + K])] = mi
    return out
