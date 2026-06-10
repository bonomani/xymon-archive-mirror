"""Golden tests for fold.py (server-side, content-proven quote folding).

The fixture is the real 6-message "Modernized HTML5 Xymon in my fork" thread
(June 2026): a Gmail `>`-quoting pair, and two Outlook/German replies whose
quotes carry NO markers at all (Von:/Gesendet: header block + gateway WARNUNG
banner) -- the exact class the client-side folder could not handle.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lxml import html as lhtml

from fold import fold_thread
from render_body import body_to_html

FIX = os.path.join(os.path.dirname(__file__), "fixtures",
                   "fold_html5_thread.json")


def _thread():
    msgs = json.load(open(FIX))
    bodies = [body_to_html(m["body"], m["body_html"]) for m in msgs]
    return msgs, bodies, fold_thread(bodies, [m["from_name"] for m in msgs])


def _visible(folded_html):
    """Text outside any <details class=q> fold."""
    root = lhtml.fromstring(f"<div>{folded_html}</div>")
    for d in root.findall(".//details"):
        d.drop_tree()
    return re.sub(r"\s+", " ", root.text_content()).strip()


def _folds(folded_html):
    root = lhtml.fromstring(f"<div>{folded_html}</div>")
    return root.findall(".//details")


def test_thread_starter_untouched():
    _, bodies, folded = _thread()
    assert folded[0] == bodies[0]          # nothing earlier -> nothing to fold


def test_each_reply_gets_one_fold_with_provenance():
    msgs, _, folded = _thread()
    for m, fh in zip(msgs[1:], folded[1:]):
        dets = _folds(fh)
        assert len(dets) == 1, m["from_name"]
        label = dets[0].findtext('.//span[@class="meta"]')
        assert label and label.strip().startswith("quoted from "), label


def test_new_text_stays_visible():
    _, _, folded = _thread()
    expect = [
        "For what it's worth, I think it looks nicer.",   # Jaime
        "I agree Jaime, the UI is a significant",          # spiderr
        "Personally, I run the server on FreeBSD.",        # Jaime
        "wow – it looks like it was a hell of a lot",      # Becker 1
        "Forgot to mention that I’ve used your source",    # Becker 2
    ]
    for fh, frag in zip(folded[1:], expect):
        assert frag in _visible(fh), frag


def test_quoted_history_is_folded():
    """The root message's text must NOT be visible in any reply -- including
    the Outlook ones with no quote markers."""
    _, _, folded = _thread()
    for fh in folded[1:]:
        vis = _visible(fh)
        assert "Thanks to all the great work" not in vis
        assert "complete purge of Web 1.0" not in vis


def test_outlook_furniture_swept_into_fold():
    """Von:/Gesendet: header block and the WARNUNG gateway banner belong to
    the quote, not to the visible message."""
    _, _, folded = _thread()
    becker1, becker2 = _visible(folded[4]), _visible(folded[5])
    for vis in (becker1, becker2):
        assert "Gesendet:" not in vis
        assert "WARNUNG" not in vis
    # but Becker's own sign-off stays visible
    assert "Regards Christian" in re.sub(r"\s+", " ", becker1)


def test_no_message_hollowed_out():
    """Every folded message keeps at least a few words of its own visible."""
    _, _, folded = _thread()
    for fh in folded:
        assert len(_visible(fh).split()) >= 3


def test_unfoldable_input_passes_through():
    bodies = ["<div class=pt><pre>hello world one</pre></div>",
              "<div class=pt><pre>totally different text here</pre></div>"]
    out = fold_thread(bodies, ["a", "b"])
    assert out == bodies                   # nothing duplicated -> unchanged


def _last_msg_views(fixture):
    """Fold a fixture thread; return (visible_text, folded_text) of its
    last message."""
    msgs = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures",
                                       fixture)))
    bodies = [body_to_html(m["body"], m["body_html"]) for m in msgs]
    folded = fold_thread(bodies, [m["from_name"] for m in msgs])
    root = lhtml.fromstring(f"<div>{folded[-1]}</div>")
    ftxt = " ".join(re.sub(r"\s+", " ", d.text_content())
                    for d in root.findall(".//details"))
    return _visible(folded[-1]), ftxt


def test_bounded_fold_spares_trailing_disclaimer():
    """A top-post whose tail ends with an UNCOVERED corporate disclaimer:
    the fold must stop at the last quoted line (bounded), folding the quote
    (incl. its short table lines) while the author's words and the trailing
    disclaimer stay visible. Real case: Scot Kreienkamp, thread 73be15218202."""
    vis, ftxt = _last_msg_views("fold_disclaimer_thread.json")
    assert "Excellent progress" in vis                       # author's reply
    assert "This message is intended only for" in vis        # trailing disclaimer
    assert "Alpine: 3.19/3.20" not in vis                    # quoted build matrix
    assert "Alpine: 3.19/3.20" in ftxt
    assert "PCRE2" not in vis                                # quoted prose


def test_hollow_guard_tries_next_candidate():
    """A falsely-covered line near the top must not abort folding: the guard
    skips to the NEXT candidate instead of giving up, so the real quote folds
    and the author's new text (incl. fresh command output) stays visible.
    Real case: Becker Christian, thread d264e9e12ab2."""
    vis, ftxt = _last_msg_views("fold_uptime_thread.json")
    assert "The output of uptime is" in vis                  # author's new text
    assert "13:02:45 up 23:03" in vis                        # fresh uptime output
    assert "WARNUNG" not in vis                              # quoted banner
    assert "LC_NUMERIC=de_DE" not in vis                     # quoted locale dump
    assert "LC_NUMERIC=de_DE" in ftxt


def test_deep_quoted_mailman3_footer_dropped():
    """Wrapped, mailto-mangled Mailman 3 footer lines inside deep quotes are
    boilerplate, never content -- the per-line filter must drop every form."""
    from render_body import _is_footer_line
    for line in (
        ">     To unsubscribe send an email to xymon-leave@xymon.com <mailto:xymon-",
        "> >      > leave@xymon.com <mailto:leave@xymon.com>>",
        "> Xymon mailing list -- xymon@xymon.com<mailto:xymon@xymon.com>",
        ">     <mailto:xymon@xymon.com>>",
        "To unsubscribe send an email to xymon-leave@xymon.com<mailto:xymon-leave@xymon.com>",
    ):
        assert _is_footer_line(line), line
    for line in (                            # real content must survive
        "Please leave a comment on the PR if you disagree.",
        "send an email to me directly instead",
        "mailto links in the docs are broken",
    ):
        assert not _is_footer_line(line), line


def test_inline_answers_inside_long_quote_stay_visible():
    """THE swallow bug: short inline answers inside a long quote pass the 85%
    tail-coverage ratio (they are under the 15% noise allowance), so the fold
    ate them. An uncovered non-furniture line of >=4 words is FOREIGN prose
    now -- the fold must end before it. Real case: Gary Baluha replying
    point-by-point inside Ralph Mitchell's quoted list (thread 298c6f0e0954);
    his answers were folded to a hollow message."""
    vis, ftxt = _last_msg_views("fold_inline_reply_thread.json")
    assert "I think my new method would handle that" in vis   # inline answer 1
    assert "2-5: Wow..." in vis                                # inline answer 2
    assert "really weird websites out there" in vis            # closing remark
    assert len(vis.split()) >= 30


def test_foreign_prose_is_per_line_not_ratio():
    """A 7-word reply wedged between two covered blocks must block the fold
    over it even though it is far below 15% of the tail."""
    parent = ("<div class=pt><pre>alpha beta gamma delta epsilon zeta eta theta "
              "iota kappa\nlambda mu nu xi omicron pi rho sigma tau upsilon\n"
              "phi chi psi omega one two three four five six</pre></div>")
    child = ("<div class=pt><pre>my own intro line says hello here\n"
             "alpha beta gamma delta epsilon zeta eta theta iota kappa\n"
             "lambda mu nu xi omicron pi rho sigma tau upsilon\n"
             "this short answer is entirely mine here\n"
             "phi chi psi omega one two three four five six</pre></div>")
    out = fold_thread([parent, child], ["p", "c"])
    vis = _visible(out[1])
    assert "this short answer is entirely mine" in vis
    assert "my own intro line" in vis


def test_control_chars_do_not_break_folding():
    """A NUL (or other C0 control char) pasted into a mail must not abort the
    fold: lxml refuses to serialize such text, which used to make the whole
    message silently render unfolded (11 corpus messages). They are stripped
    before parsing; the quote folds and the marker survives sans control char."""
    import fold as fold_mod
    parent = ("<div class=pt><pre>the quick brown fox jumps over the lazy dog "
              "and keeps running far away into the night</pre></div>")
    child = ("<div class=pt><pre>thanks a lot \x00for the hint everyone\n"
             "On Mon someone wrote:\n"
             "the quick brown fox jumps over the lazy dog "
             "and keeps running far away into the night</pre></div>")
    before = fold_mod.STATS["errors"]
    out = fold_thread([parent, child], ["a", "b"])
    assert fold_mod.STATS["errors"] == before      # no swallowed exception
    assert "<details" in out[1]                    # the quote folded
    vis = _visible(out[1])
    assert "thanks a lot" in vis and "\x00" not in vis
    assert "quick brown fox" not in vis


def test_inline_reply_not_swallowed():
    """A reply interleaving NEW answers between quoted blocks fails tail
    validation -> no server fold (the conservative client path handles it)."""
    parent = ("<div class=pt><pre>first question about disks is here for you\n"
              "second question about network is here for you too</pre></div>")
    child = ("<div class=pt><pre>first question about disks is here for you\n"
             "my answer: tune the disk elevator settings now\n"
             "second question about network is here for you too\n"
             "my answer: jumbo frames help a lot in this case</pre></div>")
    out = fold_thread([parent, child], ["p", "c"])
    assert "<details" not in out[1]
