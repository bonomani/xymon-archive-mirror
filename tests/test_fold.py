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
