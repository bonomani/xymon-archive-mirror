"""Body rendering: a stored plain-text or HTML message body -> the HTML
shown on a message page. Quote nesting (one <blockquote> per ">"),
RFC 3676 format=flowed reflow, URL linking, and stripping of Pipermail
scrub notes / Mailman list footers + HTML tag balancing. Pure in/out.
"""
from __future__ import annotations

import html
import re


_URL = re.compile(r"(https?://[^\s<>()]+[^\s<>().,;:'\"\]])")
_QUOTE = re.compile(r"^\s*>")
# Leading email quote markers ("> ", ">> ", "> > > ", ...). The captured group's
# ">" count is the nesting depth; render it as that many vertical bars.
_QUOTE_PREFIX = re.compile(r"^((?:[ \t]*>)+)[ \t]?")
# Stored body_html (sanitized at ingest) can still carry unclosed tags from the
# source email; left open they swallow whatever follows on the page (e.g. the
# thread nav). Append the missing closing tags without altering the content.
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>")
_VOID_HTML = {"br", "hr", "img", "wbr", "col", "area", "input", "meta", "link"}


def balance_html(h: str) -> str:
    stack: list[str] = []
    for m in _TAG.finditer(h or ""):
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            if name in stack:                 # pop down to the matching open
                while stack.pop() != name:
                    pass
        elif not selfclose and name not in _VOID_HTML:
            stack.append(name)
    return (h or "") + "".join(f"</{t}>" for t in reversed(stack))
# Pipermail appends "scrub notes" for stripped attachments at the end of the
# plain-text body. The useful attachments are shown in the Attachments box and
# HTML-only content is recovered into body_html, so these notes are just noise.
# A note may start with a "---- next part ----" delimiter or directly with the
# "An HTML attachment was scrubbed" line (HTML-only mail). Both forms must NOT
# be matched when quoted (a leading ">"), so a reply isn't truncated.
_SCRUB_DELIM = re.compile(r"\n*[ \t]*-{2,}[ \t]*next part[ \t]*-{2,}", re.I)
_SCRUB_LINE = re.compile(
    r"(?:\A|\n)[ \t]*An?\b[^\n]*was scrubbed", re.I)


def strip_scrub_notes(text: str) -> str:
    if not text:
        return ""
    starts = [m.start() for m in
              (_SCRUB_DELIM.search(text), _SCRUB_LINE.search(text)) if m]
    if starts:
        s = min(starts)
        if "scrubbed" in text[s:].lower():
            return text[:s].rstrip() + "\n"
    return text


# Mailman list footers appended to the end of the body: the classic
# "____ / <name> mailing list / <addr> / .../listinfo/..." block, and the
# newer "Links:\n------\n[1] http://.../listinfo/..." footnote form.
_FOOTER = re.compile(
    r"\n_{5,}[ \t]*\n.{0,400}?listinfo[^\n]*\s*\Z", re.S | re.I)
_LINKS_FOOTER = re.compile(
    r"\n+[ \t]*Links:[ \t]*\n-{3,}[ \t]*\n(?:[ \t]*\[\d+\][^\n]*\n?)+\s*\Z",
    re.I)
# Old Hobbit-era unsubscribe footer ("To unsubscribe from the hobbit list,
# send an e-mail to <addr>") and similar trailing list instructions.
_UNSUB = re.compile(
    r"\n+[ \t]*To unsubscribe from[^\n]*(?:\n[^\n]*){0,2}\s*\Z", re.I)


# New Mailman 3 footer: a rule line (underscores / dashes / <hr>) then
# "Xymon mailing list -- <addr>" and an optional "To unsubscribe send an email
# to <addr>" line. In deep reply chains it is quoted at EVERY level, so remove
# all occurrences (plain or HTML, quoted or not, full or 2-line) -- it is list-
# injected boilerplate, never message content. Lines may be wrapped/quoted with
# arbitrary markup, so the gaps tolerate interleaved tags and quote markers.
# A footer line is an address (@ or pipermail "<name> at <domain>"), a listinfo
# URL, or an unsubscribe instruction.
_FADDR = (r"(?:<a\b[^>]*>[^<]*</a>|[^\s<]+@[^\s<]+|"
          r"[\w.+-]+ at [\w.-]+\.[a-z]{2,})")
# Any token bearing "/listinfo/" -- scheme may be missing (obfuscator ate the
# "http"), so don't require it; "/listinfo/" alone is unambiguous boilerplate.
_FURL = r"[^\s<]*/listinfo/[^\s<]*"
_FLINE = r"(?:" + _FADDR + r"|" + _FURL + r"|To unsubscribe[^<\n]*)"
# A list footer: a rule line (underscores/dashes/<hr>), the "<name> mailing
# list" header, then any run of footer lines (address / listinfo URL /
# unsubscribe), in old or new Mailman form.
_FOOTER_RULE = re.compile(r"(?:[_-]{5,}|<hr\s*/?>)", re.I)
_FSEP_TOKEN = re.compile(
    r"</?[a-z][a-z0-9]*[^>]*>|[\s>]|&nbsp;|&gt;", re.I)
_LIST_NAME = re.compile(r"(?:Xymon|Hobbit) mailing list\b[^<\n]*", re.I)
_FOOTER_LINE = re.compile(_FLINE, re.I)
# Any leftover listinfo URL line (quoted footers whose header was already cut,
# including the mangled "Xymon mailing <addr>://.../listinfo/..." remnant).
_LISTINFO_LINE = re.compile(
    r"(?:</?[a-z][^>]*>|[\s>]|&gt;|&nbsp;)*"
    r"(?:(?:Xymon|Hobbit)\s+mailing\s+)?" + _FURL, re.I)
# Old Hobbit-era unsubscribe footer ("To unsubscribe from the <name> list,
# send an e-mail to <addr>"). It also appears inline/quoted, not just at the
# end, so strip it (instruction + address) at every occurrence.
_UNSUB_LINE = re.compile(
    r"To unsubscribe from the \w+ list,\s*send an e-?mail to"
    r"(?:[\s>]|&gt;|&nbsp;|<br\s*/?>)*"
    r"(?:<a\b[^>]*>[^<]*</a>|[^\s<]+@[^\s<]+)?",
    re.I)
# NOTE: no "|\xa0" alternative -- \s already matches NBSP in unicode mode, and
# the overlap made the star ambiguous (two parses per \xa0 -> exponential
# backtracking; a 2019 Gmail reply with "\xa0<br>" runs hung the full render).
# The remaining alternatives are disjoint at their first character, so the
# star is deterministic.
_EMPTY_WRAP = re.compile(
    r"<(pre|blockquote|div|p)>(?:\s|&nbsp;|<br\s*/?>)*</\1>", re.I)


def strip_list_footers(s: str) -> str:
    s = _strip_list_footer_blocks(s or "")
    s = _UNSUB_LINE.sub("", s)
    s = _LISTINFO_LINE.sub("", s)
    for _ in range(4):                 # collapse wrappers the strip emptied
        new = _EMPTY_WRAP.sub("", s)
        if new == s:
            break
        s = new
    return s


def _strip_list_footer_blocks(s: str) -> str:
    """Linear scanner for inline/quoted Mailman footer blocks.

    The former nested regex could backtrack for minutes when a valid footer
    was followed by a long HTML disclaimer. Tokens between footer lines have
    disjoint, anchored matches here, so each input character is considered a
    bounded number of times.
    """
    out = []
    copied = search = 0
    while True:
        rule = _FOOTER_RULE.search(s, search)
        if rule is None:
            out.append(s[copied:])
            return "".join(out)
        pos = rule.end()
        while True:
            sep = _FSEP_TOKEN.match(s, pos)
            if sep is None:
                break
            pos = sep.end()
        header = _LIST_NAME.match(s, pos)
        if header is None:
            search = rule.end()
            continue

        end = header.end()
        while True:
            pos = end
            while True:
                sep = _FSEP_TOKEN.match(s, pos)
                if sep is None:
                    break
                pos = sep.end()
            line = _FOOTER_LINE.match(s, pos)
            if line is None:
                break
            end = line.end()
        out.append(s[copied:rule.start()])
        copied = search = end


# AVG / anti-virus scanner footers appended to 2000s-era mail ("No virus found
# in this incoming message. / Checked by AVG - www.avg.com / Version: ... Virus
# Database: ... Release Date: ..."). Pure noise; strip every occurrence, quoted
# (leading ">") or not, anywhere in the body.
_AVG = re.compile(
    r"(?im)"
    r"(?:^[ \t>]*No virus found in this[^\n]*\n)?"        # optional intro line
    r"^[ \t>]*Checked by AVG\b[^\n]*\n"                   # anchor (always present)
    r"(?:^[ \t>]*(?:Version|Virus Database|Release Date)\b[^\n]*\n?)*")
#   anchored on "Checked by AVG" so a message that merely talks about a virus is
#   left untouched; intro and Version/Database/Release lines are optional and may
#   wrap onto several lines, quoted (">") or not. Repeated blocks all go.


def strip_footer(text: str) -> str:
    text = _AVG.sub("", text or "")
    text = _LINKS_FOOTER.sub("", text)
    text = _UNSUB.sub("", text)
    text = _FOOTER.sub("", text)
    text = strip_list_footers(text)
    return text.rstrip() + "\n"


def strip_html_footer(h: str) -> str:
    """Drop the list's own appended footer from an HTML body (the last
    unquoted block starting an unsubscribe/mailing-list instruction). Quoted
    footers (a leading &gt;) inside replies are kept."""
    for m in re.finditer(r"To unsubscribe|Manage your subscription",
                         h, re.I):
        starts = [h.rfind(t, 0, m.start()) for t in ("<p>", "<div>", "<li>")]
        b = max(starts)
        if b < 0:
            continue
        if "&gt;" in h[b:m.start()]:          # quoted -> keep
            continue
        return h[:b].rstrip()
    return h


# Per-line footer filter for plain text. In deep reply chains the list footer
# is quoted (">>>>"), wrapped and broken into footnote refs, so whole-text
# regexes miss it. After stripping leading quote markers and a trailing "[N]"
# footnote marker, a line that is pure list boilerplate is dropped. Legitimate
# footnotes (e.g. "[1] http://logstash.net") are kept -- only the list's own
# address / listinfo URL / mailing-list header / rule line go.
_DEQUOTE = re.compile(r"^(?:\s*>)+\s?")
_FOOTNOTE = re.compile(r"\s*\[\d+\]\s*$")
_FOOTER_LINE = re.compile(
    r"(?:"
    r"[_-]{5,}"                                       # rule line
    r"|(?:Xymon|Hobbit)\s+mailing\s+list(?:\s*--.*)?"  # header, incl. M3 "-- addr"
    r"|(?:mailto:)?(?:Xymon|Hobbit)[\w.-]*\s+at\s+[\w.-]+\.\w+"  # "x at dom"
    r"|(?:mailto:)?(?:xymon|hobbit)[\w.+-]*@[\w.-]+"  # x@dom list address
    r"|To unsubscribe send an e-?mail to\b.*"         # M3 instruction (any wrap)
    r"|[\w-]*leave@[\w.-]*(?:xymon|hobbit)[\w.-]*\b.*"  # wrapped leave@ remnant
    r"|(?:<mailto:[^>\s]*>*\s*)+"                     # mailto-link debris line
    r"|\S*/listinfo/\S*"                              # listinfo URL
    r"|\[\d+\]\s*mailto:(?:Xymon|Hobbit)[\w.-]*\s+at\s+[\w.-]+\.\w+"  # [n] def
    r"|\[\d+\]\s*\S*/listinfo/\S*"                    # [n] listinfo def
    r")(?:\s*<mailto:[^>\s]*>*)*\s*$",                # trailing mailto junk
    re.I)


def _is_footer_line(line: str) -> bool:
    content = _FOOTNOTE.sub("", _DEQUOTE.sub("", line)).strip()
    return bool(content) and bool(_FOOTER_LINE.match(content))


_FLOW_Q = re.compile(r"^(>+) ?")


def _unflow(text: str) -> str:
    """RFC 3676 format=flowed -> reflowed paragraphs. A line ending in a space
    is a soft wrap (join with the next); a line without is a hard break. Lines
    only join within the same quote depth, and "-- " (signature) never joins."""
    para: list[list] = []                     # [depth, text, prev-was-soft]
    for raw in (text or "").split("\n"):
        m = _FLOW_Q.match(raw)
        depth = len(m.group(1)) if m else 0
        content = raw[m.end():] if m else raw
        soft = content.endswith(" ") and content != "-- "
        if para and para[-1][0] == depth and para[-1][2]:
            para[-1][1] += content
            para[-1][2] = soft
        else:
            para.append([depth, content, soft])
    return "\n".join((">" * d + (" " if d else "") + c) for d, c, _ in para)


def _is_flowed(text: str) -> bool:
    """Heuristic for RFC 3676 format=flowed. Either enough lines overall end in a
    trailing space, OR the soft-wrap pattern is clear among the long (wrappable)
    lines. The latter catches a short reply sitting on top of a deep quoted chain,
    where the many short hard-break lines dilute the overall ratio below the
    threshold (e.g. Stef Coene's replies in the XymonPSClient thread)."""
    lines = [ln for ln in (text or "").split("\n")
             if ln.strip() and ln.strip("> ") != "--"]
    if len(lines) < 4:
        return False
    if sum(1 for ln in lines if ln.endswith(" ")) >= max(3, len(lines) * 0.3):
        return True
    long_lines = [ln for ln in lines if len(ln.rstrip()) >= 50]
    soft_long = sum(1 for ln in long_lines if ln.endswith(" "))
    return len(long_lines) >= 4 and soft_long >= 3 and soft_long >= len(long_lines) * 0.33


def render_plain(text: str) -> str:
    """Plain-text body -> <pre>/<blockquote> with clickable URLs. Each run of
    quoted lines becomes a real nested <blockquote> (one level per ">"), so the
    quote shows the same continuous border bar as an HTML reply."""
    # Normalize CRLF/CR first: a stray \r left after splitting on \n renders as
    # an extra line break (doubling every quoted line), and it also hides the
    # format=flowed trailing space from _is_flowed.
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = strip_footer(strip_scrub_notes(text))
    if _is_flowed(text):                       # honor RFC 3676 format=flowed
        text = _unflow(text)
    out: list[str] = []
    pre: list[str] = []                        # lines buffered for the open <pre>
    depth = 0
    blanks = 0

    def flush_pre():
        if pre:
            if any(ln for ln in pre):          # skip a block of only blank lines
                out.append("<pre>" + "\n".join(pre) + "</pre>")
            pre.clear()

    # Parse to [depth, content, raw], then re-attach wrap-orphans: a quoted line
    # that a mailer soft-wrapped can drop its ">" marker on the continuation, so
    # the tail word lands at depth 0 between two same-depth lines and juts out of
    # the blockquote. A non-blank depth-0 line wedged between two non-blank lines
    # of the same depth D>0 (no blank gap, prior line cut mid-sentence) is that
    # orphan -> promote it back to D.
    parsed = []
    for line in (text or "").split("\n"):
        qm = _QUOTE_PREFIX.match(line)
        d, content = (qm.group(1).count(">"), line[qm.end():]) if qm \
            else (0, line)
        parsed.append([d, content, line])
    for i in range(1, len(parsed)):
        d0, c0 = parsed[i][0], parsed[i][1]
        if not c0.strip():
            continue
        dp, pc = parsed[i - 1][0], parsed[i - 1][1]
        # a mailer soft-wrap can drop one or more ">" markers on a quoted line's
        # continuation, leaving it shallower than the line it continues. If the
        # previous (deeper) line was cut mid-sentence and this one starts
        # lowercase, it is that continuation -> restore its depth. The lowercase
        # guard avoids demoting a genuine de-indent (which starts a new sentence).
        if (dp > d0 and pc.strip()
                and not pc.rstrip().endswith((".", "!", "?", ":"))
                and c0.lstrip()[:1].islower()):
            parsed[i][0] = dp

    # Some clients put a bullet marker alone on its line and the item text a
    # blank line below ("  *\n\n    Foo"). Re-join into a single "  • Foo".
    _BULLET = re.compile(r"^(\s*)([*+•-])\s*$")
    joined, i = [], 0
    while i < len(parsed):
        m = _BULLET.match(parsed[i][1])
        if m:
            k = i + 1
            while k < len(parsed) and not parsed[k][1].strip():
                k += 1
            if k < len(parsed) and parsed[k][0] == parsed[i][0]:
                joined.append([parsed[i][0],
                               m.group(1) + "• " + parsed[k][1].strip(),
                               parsed[i][2]])
                i = k + 1
                continue
        joined.append(parsed[i])
        i += 1
    parsed = joined

    for d, content, line in parsed:
        if _is_footer_line(line):
            continue
        # A line is "blank" once quote markers/whitespace are removed. Cap runs
        # at 2, plain or quoted.
        if not line.strip(" >\t"):
            blanks += 1
            if blanks > 2:
                continue
        else:
            blanks = 0
        if d != depth:                         # enter/leave quote levels
            flush_pre()
            out.append("<blockquote>" * (d - depth) if d > depth
                       else "</blockquote>" * (depth - d))
            depth = d
        pre.append(_URL.sub(r'<a href="\1" rel="noopener nofollow">\1</a>',
                            html.escape(content)))
    flush_pre()
    if depth:
        out.append("</blockquote>" * depth)
    return "<div class=pt>" + "".join(out) + "</div>"


def body_to_html(body: str, body_html: str) -> str:
    """A message's stored body -> display HTML: the (sanitized) HTML alternative
    when present, else the plain-text body. Shared by the per-message page and
    the thread page so both render identically. We render the sender's HTML as-is
    (e.g. a numbered list split by code blocks stays as authored) -- an archive is
    faithful to what was sent, not "corrected" to a guessed intent."""
    if body_html:
        cleaned = strip_list_footers(strip_html_footer(body_html))
        return f"<div class=md>{balance_html(cleaned)}</div>"
    return render_plain(body)
