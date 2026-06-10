"""Unit tests for the bug-prone pure functions of the archive pipeline.

These lock in fixes we actually shipped: charset mojibake, catastrophic regex
backtracking, attachment-address leaks, the @xymon.com override path, name
normalisation, and quoted-reply / bullet rendering.
"""
import time
from email import message_from_string

import hashlib
import sqlite3

import mailstore
import obfuscate
import generate
import render_body
import vaultcache
import threads
import dbhash

SALT = b"unit-test-salt"


# --- mailstore.decode_payload -------------------------------------------------

def test_decode_utf8():
    assert mailstore.decode_payload("café".encode("utf-8"), "utf-8") == "café"


def test_decode_cp1252_fallback():
    # 0x96 is an en-dash in cp1252, invalid utf-8; no declared charset -> sniff
    assert mailstore.decode_payload(b"a\x96b", None) == "a–b"


def test_decode_meta_charset_hint():
    payload = b'<meta charset="windows-1252">x\x96y'
    assert "–" in mailstore.decode_payload(payload, None)


def test_decode_declared_wins():
    assert mailstore.decode_payload(b"a\x96b", "cp1252") == "a–b"


# --- mailstore.sanitize_html / _EMPTY_BLOCK (no catastrophic backtracking) -----

def test_empty_block_no_backtracking():
    s = "<div>" + ("&nbsp;" * 400) + "</div>"
    t0 = time.time()
    out = mailstore.sanitize_html(s)
    assert time.time() - t0 < 0.5            # was O(2^n) -> minutes
    assert "div" not in out.lower()          # empty block collapsed away


# --- mailstore.html_part (recover the detached HTML body) ----------------------

def test_html_part_captures_attachment_html():
    raw = (
        "From: a@b.com\nSubject: t\n"
        'Content-Type: multipart/mixed; boundary="B"\n\n'
        "--B\nContent-Type: text/plain\n\nplain\n"
        "--B\nContent-Type: text/html\n"
        'Content-Disposition: attachment; filename="attachment.html"\n\n'
        "<html><body><p>Hello <b>world</b></p></body></html>\n--B--\n"
    )
    out = mailstore.html_part(message_from_string(raw))
    assert out and "Hello" in out and "<b>world</b>" in out


# --- obfuscate.make_repl / _pseudo (pseudonymise, keep @xymon.com) -------------

def test_text_pseudonymises_real_address():
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    out = text("mail alice@example.com end")
    assert "alice@example.com" not in out and "@xymon.invalid" in out


def test_text_keeps_allowlisted_list_address():
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    assert "xymon@xymon.com" in text("ping xymon@xymon.com")
    assert "xymon-bounces@xymon.com" in text("from xymon-bounces@xymon.com")


def test_text_pseudonymises_nonallowlisted_xymon_com():
    # the whole @xymon.com domain is NOT exempt -- a personal/unknown address
    # there is pseudonymised like any other (exact allowlist, not domain-wide).
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    out = text("mail henrik@xymon.com please")
    assert "henrik@xymon.com" not in out and "@xymon.invalid" in out


def test_text_masks_obfuscated_address_forms():
    # scraper-dodging forms are reversible -> must be masked, not just plain @ (#1).
    _t, _b, text, _n, blob = obfuscate.make_repl(SALT)
    for s, leak in [
        ("0500a8c0 (at) noip.org", "noip.org"),               # (at)
        ("user [at] example.org", "example.org"),             # [at]
        ("bgmilne%40staff.telkomsa.net", "telkomsa"),         # %40
        ("safelink%7C01%7Ctschmidt%40micron.com%7C40", "micron.com"),  # %40 in URL
        ("foo (at)\n      mail.gmail.com", "mail.gmail.com"),  # wrapped (at)
        ("erdmann at daimler dot com", "daimler"),            # word dot
        ("parker AT uregina DOT ca", "uregina"),              # caps
        ('"alias"@example.org', "example.org"),               # quoted local
        ("msgid@[207.242.93.105]", "207.242"),                # domain literal
    ]:
        out = text(s)
        assert leak not in out and "@xymon.invalid" in out, (s, out)
    assert text("available at sourceforge.net") == "available at sourceforge.net"
    assert b"telkomsa" not in blob(b"a bgmilne%40staff.telkomsa.net b")


def test_text_pseudonym_exemption_requires_exact_match():
    # "xymon.invalid" merely appearing in the local part must NOT exempt a real
    # address; only an exact user-<hex>@xymon.invalid pseudonym is kept (#3).
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    out = text("victim.xymon.invalid at example.com")
    assert "example.com" not in out and "@xymon.invalid" in out
    out2 = text("mail victim.xymon.invalid@example.com")
    assert "example.com" not in out2
    kept = text("user-123456789abc@xymon.invalid")     # real pseudonym kept
    assert kept == "user-123456789abc@xymon.invalid"


def test_obfuscate_sanitizes_text_archive(tmp_path, monkeypatch):
    # a TEXT member with an address inside a gz/zip/tar is invisible to the byte
    # scanner -> sanitise the archive (scrub the member, rebuild) and publish the
    # cleaned copy; a clean archive is left untouched (#1, refined).
    import gzip as _gz
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    db = str(tmp_path / "arch.db")
    conn = mailstore.connect(db)
    leaky = _gz.compress(b"config: admin = realname@acme-corp.com\n")
    conn.execute(
        "INSERT INTO attachment (msgid, month, url, filename, content_type, "
        "size, content) VALUES (?,?,?,?,?,?,?)",
        ("<1@h>", "2024-01", "u", "dump.gz", "application/octet-stream",
         len(leaky), leaky))
    clean = _gz.compress(b"just some logs, nothing personal\n")
    conn.execute(
        "INSERT INTO attachment (msgid, month, url, filename, content_type, "
        "size, content) VALUES (?,?,?,?,?,?,?)",
        ("<1@h>", "2024-01", "u2", "ok.gz", "application/octet-stream",
         len(clean), clean))
    conn.commit()
    conn.close()
    obfuscate.obfuscate(db)
    rows = dict(sqlite3.connect(db).execute(
        "SELECT filename, content FROM attachment"))
    assert rows["dump.gz"] != obfuscate._WITHHELD       # sanitised, not withheld
    inner = _gz.decompress(rows["dump.gz"])
    assert b"acme-corp.com" not in inner and b"@xymon.invalid" in inner
    assert rows["ok.gz"] == clean                       # untouched


def test_obfuscate_withholds_binary_archive(tmp_path, monkeypatch):
    # a BINARY member carrying an address can't be cleaned safely -> withhold.
    import gzip as _gz
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    db = str(tmp_path / "bin.db")
    conn = mailstore.connect(db)
    blob = _gz.compress(b"\x00\x01\x02 realname@acme-corp.com \x00\xff\xfe data")
    conn.execute(
        "INSERT INTO attachment (msgid, month, url, filename, content_type, "
        "size, content) VALUES (?,?,?,?,?,?,?)",
        ("<1@h>", "2024-01", "u", "blob.gz", "application/octet-stream",
         len(blob), blob))
    conn.commit()
    conn.close()
    obfuscate.obfuscate(db)
    (content,) = sqlite3.connect(db).execute(
        "SELECT content FROM attachment").fetchone()
    assert content == obfuscate._WITHHELD


def test_fetch_scrubbed_html_stores_raw_bytes(tmp_path, monkeypatch):
    # the cache must keep the BYTE-EXACT response, not just decoded text (#5).
    import fetch_scrubbed_html as fsh
    RAW = b"<tt>&lt;p&gt;h\xe9llo&lt;/p&gt;</tt>\n"   # non-utf8 byte on purpose

    class _H:
        def get_content_charset(self):
            return None
    monkeypatch.setattr(fsh, "httpget", lambda u: (RAW, _H()))
    db = str(tmp_path / "a.db")
    conn = mailstore.connect(db)
    conn.execute(
        "INSERT INTO message (month, msgid, body) VALUES (?,?,?)",
        ("2024-01", "<1@h>",
         "URL: <https://x/y/attachment.html>\n----- next part -----\n"))
    conn.commit(); conn.close()
    cache = str(tmp_path / "cache.db")
    fsh.main(["--db", db, "--cache", cache, "--delay", "0"])
    rb = sqlite3.connect(cache).execute("SELECT raw_bytes FROM html").fetchone()
    assert rb is not None and bytes(rb[0]) == RAW


def test_fetch_scrubbed_html_backfills_raw_bytes(tmp_path, monkeypatch):
    # cached rows that pre-date raw_bytes are skipped by the normal loop; the
    # --backfill-raw path must fill them (#5).
    import fetch_scrubbed_html as fsh
    RAW = b"\x89raw-bytes\x00"
    monkeypatch.setattr(fsh, "httpget", lambda u: (RAW, None))
    cache = str(tmp_path / "cache.db")
    cc = sqlite3.connect(cache)
    cc.execute("CREATE TABLE html (url TEXT PRIMARY KEY, body_html TEXT, "
               "raw_html TEXT, raw_bytes BLOB)")
    cc.execute("INSERT INTO html (url, body_html) VALUES ('https://x/a.html','<p>x</p>')")
    cc.commit(); cc.close()
    db = str(tmp_path / "a.db")
    mailstore.connect(db).close()
    fsh.main(["--db", db, "--cache", cache, "--backfill-raw", "--delay", "0"])
    rb = sqlite3.connect(cache).execute("SELECT raw_bytes FROM html").fetchone()
    assert rb is not None and bytes(rb[0]) == RAW


def _att(conn, fn, content, ct="application/octet-stream"):
    conn.execute(
        "INSERT INTO attachment (msgid, month, url, filename, content_type, "
        "size, content) VALUES (?,?,?,?,?,?,?)",
        ("<1@h>", "2024-01", fn, fn, ct, len(content), content))


def test_obfuscate_redacts_phone_in_text_attachment(tmp_path, monkeypatch):
    # attachments must run redact_contact too, not just email replacement (#2).
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    db = str(tmp_path / "p.db")
    conn = mailstore.connect(db)
    _att(conn, "contact.txt", b"call 801-446-5645 or mail joe@acme.com\n", "text/plain")
    conn.commit(); conn.close()
    obfuscate.obfuscate(db)
    (c,) = sqlite3.connect(db).execute("SELECT content FROM attachment").fetchone()
    assert b"801-446-5645" not in c and b"XXX-XXX-XXXX" in c
    assert b"joe@acme.com" not in c and b"@xymon.invalid" in c


def test_obfuscate_sanitizes_dupname_zip(tmp_path, monkeypatch):
    # duplicate ZIP names must not hide the earlier (leaky) entry (#3): read by
    # ZipInfo. Both entries get cleaned -> no address survives.
    import io as _io, zipfile as _zip
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    buf = _io.BytesIO()
    z = _zip.ZipFile(buf, "w")
    z.writestr("dup.txt", b"secret leak@evil.com\n")
    z.writestr("dup.txt", b"harmless\n")
    z.close()
    db = str(tmp_path / "z.db")
    conn = mailstore.connect(db)
    _att(conn, "d.zip", buf.getvalue(), "application/zip")
    conn.commit(); conn.close()
    obfuscate.obfuscate(db)
    (c,) = sqlite3.connect(db).execute("SELECT content FROM attachment").fetchone()
    members = obfuscate._inspect(c)
    assert members is not None
    assert not any(obfuscate._member_unsafe(m) for m in members)   # leak gone
    assert b"leak@evil.com" not in b"".join(members)


def test_obfuscate_withholds_overdepth_archive(tmp_path, monkeypatch):
    # nested beyond the depth cap can't be verified -> withhold (#3/#6).
    import gzip as _gz
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    data = b"deep joe@evil.com\n"
    for _ in range(6):
        data = _gz.compress(data)
    db = str(tmp_path / "deep.db")
    conn = mailstore.connect(db)
    _att(conn, "deep.gz", data)
    conn.commit(); conn.close()
    obfuscate.obfuscate(db)
    (c,) = sqlite3.connect(db).execute("SELECT content FROM attachment").fetchone()
    assert c == obfuscate._WITHHELD


def test_obfuscate_withholds_archive_bomb_without_oom(tmp_path, monkeypatch):
    # the expanded-size guard must fire DURING decompression, so a bomb is
    # withheld without being fully materialised (#3).
    import gzip as _gz
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    monkeypatch.setattr(obfuscate, "_ARCH_MAX_BYTES", 1000)
    bomb = _gz.compress(b"A" * 5_000_000)        # 5 MB -> well over the 1000 cap
    assert len(bomb) < 50_000                     # tiny compressed (bomb-shaped)
    db = str(tmp_path / "bomb.db")
    conn = mailstore.connect(db)
    _att(conn, "bomb.gz", bomb)
    conn.commit(); conn.close()
    obfuscate.obfuscate(db)
    (c,) = sqlite3.connect(db).execute("SELECT content FROM attachment").fetchone()
    assert c == obfuscate._WITHHELD
    # the bounded gunzip never returns the full 5 MB
    assert obfuscate._bounded_gunzip(bomb, 1000) is None


def test_obfuscate_withholds_over_member_limit(tmp_path, monkeypatch):
    # too many members (archive-bomb guard) -> withhold (#6).
    import io as _io, zipfile as _zip
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    monkeypatch.setattr(obfuscate, "_ARCH_MAX_MEMBERS", 3)
    buf = _io.BytesIO()
    z = _zip.ZipFile(buf, "w")
    for i in range(10):
        z.writestr(f"f{i}.txt", b"x@acme.com\n")
    z.close()
    db = str(tmp_path / "many.db")
    conn = mailstore.connect(db)
    _att(conn, "many.zip", buf.getvalue(), "application/zip")
    conn.commit(); conn.close()
    obfuscate.obfuscate(db)
    (c,) = sqlite3.connect(db).execute("SELECT content FROM attachment").fetchone()
    assert c == obfuscate._WITHHELD


def test_obfuscate_scrubs_attachment_metadata(tmp_path, monkeypatch):
    # filename is rendered and url ships in the published DB -> an address in
    # either must be pseudonymised, not just msgid/content.
    monkeypatch.setenv("OBFUSCATE_SALT", "test-salt")
    db = str(tmp_path / "a.db")
    conn = mailstore.connect(db)
    conn.execute(
        "INSERT INTO attachment (msgid, month, url, filename, content_type, "
        "size, content) VALUES (?,?,?,?,?,?,?)",
        ("<1@h>", "2024-01", "https://x/d?to=joe@acme.com",
         "resume_jane.doe@gmail.com.pdf", "application/pdf", 3, b"abc"))
    conn.commit()
    conn.close()
    obfuscate.obfuscate(db)
    fn, url = sqlite3.connect(db).execute(
        "SELECT filename, url FROM attachment").fetchone()
    assert "jane.doe@gmail.com" not in fn and "@xymon.invalid" in fn
    assert "joe@acme.com" not in url


def test_pseudo_deterministic():
    assert obfuscate._pseudo("x@y.com", SALT) == obfuscate._pseudo("x@y.com", SALT)
    assert obfuscate._pseudo("x@y.com", SALT) != obfuscate._pseudo("z@y.com", SALT)


def test_text_at_address_glued_phone():
    # a list footer that glues a phone straight onto the TLD must NOT defeat the
    # "user at host" matcher (a digit after a letter is not a \b) -- regression.
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    out = text("Developermichael.beatty at sherwin.com216-515-4000")
    assert "sherwin.com" not in out and "@xymon.invalid" in out


def test_blob_at_address_glued_phone():
    _t, _b, _text, _name, blob = obfuscate.make_repl(SALT)
    out = blob(b"> Developermichael.beatty at sherwin.com216-515-4000")
    assert b"sherwin.com" not in out and b"@xymon.invalid" in out


def test_text_repairs_overcaptured_pseudonym():
    # the greedy address regex can grab junk after a pseudonym; obfuscate must
    # repair the domain back to a bare xymon.invalid, never leave a routable
    # suffix like @xymon.invalid.example.com (regression for the privacy gate).
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    for bad in ("user-123456789abc@xymon.invalid.example.com",
                "user-123456789abc@xymon.invalid.cvf",
                "user-123456789abc@xymon.invaliduser"):
        out = text(bad)
        assert "user-123456789abc@xymon.invalid" in out
        assert "example.com" not in out and ".cvf" not in out
        assert "invaliduser" not in out
    out_b = _blob(b"x user-123456789abc@xymon.invalid.example.com y")
    assert b"@xymon.invalid.example.com" not in out_b
    assert b"user-123456789abc@xymon.invalid" in out_b


def test_text_at_prose_kept():
    # the TLD boundary change must not start eating prose the stoplist protects.
    _t, _b, text, _name, _blob = obfuscate.make_repl(SALT)
    assert text("available at sourceforge.net") == "available at sourceforge.net"
    assert text("Look at xymonton.org") == "Look at xymonton.org"


# --- obfuscate._needs_attachment_redaction ------------------------------------

def test_redaction_text_type():
    assert obfuscate._needs_attachment_redaction("text/plain", b"hi")


def test_redaction_octet_stream_with_address():
    assert obfuscate._needs_attachment_redaction(
        "application/octet-stream", b"\x00 see joe@acme.com here")


def test_redaction_binary_without_address():
    assert not obfuscate._needs_attachment_redaction(
        "image/png", b"\x89PNG\r\n\x00\x00 binary blob")


def test_redaction_empty():
    assert not obfuscate._needs_attachment_redaction("text/plain", b"")


# --- generate._clean_name -----------------------------------------------------

def test_clean_name_comma_swap():
    assert generate._clean_name("Root, Paul T") == "Paul T Root"


def test_clean_name_allcaps_surname():
    assert generate._clean_name("Cédric BRINER") == "Cédric Briner"


def test_clean_name_lowercase():
    assert generate._clean_name("deepak deore") == "Deepak Deore"


def test_clean_name_initials():
    assert generate._clean_name("J.c. Cleaver") == "J.C. Cleaver"


def test_clean_name_particles_kept_lower():
    assert generate._clean_name("Stefan van der Walt") == "Stefan van der Walt"


def test_clean_name_mixed_case_preserved():
    assert generate._clean_name("Scot McConnell") == "Scot McConnell"


# --- mailstore: single month-name authority ------------------------------------

def test_month_order_is_inverse_of_month_names():
    # both directions derive from one table, so they can never disagree
    assert mailstore.MONTH_ORDER == {
        n: int(k) for k, n in mailstore._MONTH_NAMES.items()}
    assert generate.MONTH_ORDER is mailstore.MONTH_ORDER


def test_month_key_sorts_and_tolerates_garbage():
    assert mailstore.month_key("2024-January") == (2024, 1)
    assert mailstore.month_key("2005-December") == (2005, 12)
    assert mailstore.month_key("nonsense") == (0, 0)
    assert mailstore.month_key("2024-Smarch") == (2024, 0)


# --- generate: page chrome, SEO scaffolding -----------------------------------

def test_threads_page_no_literal_unicode_escape():
    # the HTML half of the Threads template once leaked a literal "…"
    # into the visible page text (the JS half may legitimately use JS escapes)
    assert "\\u" not in generate._THREADS_PAGE.split("<script>")[0]


def test_page_escapes_meta_description_and_title_once():
    p = generate.page('A & B', 'body', desc='He said "x" & left')
    assert 'content="He said &quot;x&quot; &amp; left"' in p
    assert "<title>A &amp; B</title>" in p          # escaped exactly once


def test_page_canonical_only_with_base():
    old = generate._BASE
    try:
        generate._BASE = ""
        assert "rel=canonical" not in generate.page("t", "b", canon="x.html")
        generate._BASE = "https://example.org/site"
        assert ("<link rel=canonical href='https://example.org/site/x.html'>"
                in generate.page("t", "b", canon="x.html"))
    finally:
        generate._BASE = old


def test_github_base_derivation(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "Some-Org/some-repo")
    assert generate._github_base() == "https://some-org.github.io/some-repo"
    monkeypatch.setenv("GITHUB_REPOSITORY", "User/user.github.io")
    assert generate._github_base() == "https://user.github.io"
    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert generate._github_base() == ""


def test_sitemap_single_and_chunked(tmp_path):
    old = generate._BASE
    try:
        generate._BASE = "https://example.org/site"
        generate._write_sitemaps(tmp_path, ["", "a.html"])
        sm = (tmp_path / "sitemap.xml").read_text("utf-8")
        assert "<urlset" in sm
        assert "<loc>https://example.org/site/a.html</loc>" in sm
        assert "<loc>https://example.org/site/</loc>" in sm
        generate._write_sitemaps(
            tmp_path, [f"m{i}.html" for i in range(7)], chunk=3)
        idx = (tmp_path / "sitemap.xml").read_text("utf-8")
        assert "<sitemapindex" in idx
        assert (tmp_path / "sitemap-3.xml").exists()
        # a re-run with fewer parts prunes the stale ones
        generate._write_sitemaps(tmp_path, ["a.html"])
        assert not (tmp_path / "sitemap-1.xml").exists()
    finally:
        generate._BASE = old


# --- render_body.render_plain (bullets + wrapped-quote re-attach) -----------------

_AVG3 = ("No virus found in this incoming message.\n"
         "Checked by AVG - www.avg.com\n"
         "Version: 8.5.420 / Virus Database: 270.14.3 - Release Date: 10/05/09\n")


def test_strip_avg_variants():
    cases = [
        _AVG3,                                              # the canonical 3-liner
        "> " + _AVG3.replace("\n", "\n> ").rstrip("> "),    # quoted in a reply
        ("No virus found in this outgoing message.\n"       # wrapped onto 3 lines
         "Checked by AVG - www.avg.com\nVersion: 9.0\n"
         "Virus Database: 271\nRelease Date: 11/06/09\n"),
        "Checked by AVG Free Edition\nVersion: 8.5\n",      # no intro line
        _AVG3 + "\n" + _AVG3,                               # two blocks (real case)
    ]
    for c in cases:
        out = render_body.strip_footer("Body kept.\n\n" + c)
        assert "Body kept." in out
        assert "AVG" not in out and "Virus Database" not in out, c


def test_strip_avg_keeps_legit_virus_text():
    # a message that genuinely discusses a virus must NOT be touched
    body = "We caught a virus; the scanner database flagged it.\n"
    assert "virus" in render_body.strip_footer(body)


def test_render_plain_rejoins_orphan_bullet():
    out = render_body.render_plain("intro\n\n  *\n\n    Beginner tutorials\n")
    assert "• Beginner tutorials" in out


# --- threads.components -------------------------------------------------------

def test_threads_groups_by_reply_and_subject():
    rows = [
        {"id": 1, "msgid": "<a>", "in_reply_to": None, "subject": "Disk alert"},
        {"id": 2, "msgid": "<b>", "in_reply_to": "<a>", "subject": "Re: x"},
        {"id": 3, "msgid": "<c>", "in_reply_to": None, "subject": "Disk alert"},
        {"id": 4, "msgid": "<d>", "in_reply_to": None, "subject": "unrelated"},
    ]
    comp = threads.components(rows)
    sets = sorted(sorted(r["id"] for r in m) for m in comp.values())
    assert [1, 2, 3] in sets        # 2 replies to 1; 3 shares subject with 1
    assert [4] in sets              # 4 stands alone


def test_threads_short_subject_not_grouped():
    rows = [{"id": 1, "msgid": "<a>", "in_reply_to": None, "subject": "hi"},
            {"id": 2, "msgid": "<b>", "in_reply_to": None, "subject": "hi"}]
    assert len(threads.components(rows)) == 2   # 'hi' too short to thread


# --- threads.thread_ids (stable ids) -----------------------------------------

def _row(i, mid, irt=None, subj="", date=None):
    return {"id": i, "msgid": mid, "in_reply_to": irt, "subject": subj,
            "date_iso": date}


def test_thread_ids_same_thread_shares_id():
    rows = [_row(1, "<a>", None, "Disk full", "2024-01-01"),
            _row(2, "<b>", "<a>", "Re: x", "2024-01-02")]
    tids = threads.thread_ids(rows)
    assert tids["<a>"] == tids["<b>"]


def test_thread_ids_new_reply_keeps_id():
    rows = [_row(1, "<a>", None, "Disk full", "2024-01-01"),
            _row(2, "<b>", "<a>", "Re", "2024-01-02")]
    prior = threads.thread_ids(rows)
    rows2 = rows + [_row(3, "<c>", "<b>", "Re", "2024-01-03")]   # new reply
    tids = threads.thread_ids(rows2, prior=prior)
    assert tids["<a>"] == prior["<a>"]          # existing id unchanged
    assert tids["<c>"] == prior["<a>"]          # new msg joins the thread


def test_thread_ids_merge_keeps_dominant_existing():
    # two separate threads first (different subjects, no reply link)
    rows = [_row(1, "<a>", None, "Alpha topic", "2024-01-01"),
            _row(2, "<b>", None, "Beta topic", "2024-01-02")]
    prior = threads.thread_ids(rows)
    assert prior["<a>"] != prior["<b>"]
    # a later message replies to <a> AND shares <b>'s subject -> merges them
    rows2 = rows + [_row(3, "<c>", "<a>", "Beta topic", "2024-01-03")]
    tids = threads.thread_ids(rows2, prior=prior)
    assert len({tids["<a>"], tids["<b>"], tids["<c>"]}) == 1   # one thread now
    assert tids["<a>"] == prior["<a>"]          # kept anchor's existing id


def test_thread_ids_fresh_is_deterministic():
    rows = [_row(1, "<x>", None, "", "2024-01-01")]
    assert threads.thread_ids(rows)["<x>"] == threads.thread_ids(rows)["<x>"]


# --- vaultcache.restore / sync -----------------------------------------------

def _mk(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, url TEXT, data TEXT)")
    return c


def test_vaultcache_roundtrip_and_noop(tmp_path):
    build, vault = str(tmp_path / "b.db"), str(tmp_path / "v.db")
    c = _mk(build)
    c.execute("INSERT INTO t (url, data) VALUES ('u1','a'),('u2','b')")
    c.commit(); c.close()

    assert vaultcache.sync(build, vault, "t", "url") == 2       # build -> vault
    h = hashlib.md5(open(vault, "rb").read()).hexdigest()
    assert vaultcache.sync(build, vault, "t", "url") == 0       # no-op
    assert hashlib.md5(open(vault, "rb").read()).hexdigest() == h  # byte-identical

    build2 = str(tmp_path / "b2.db")
    _mk(build2).close()
    assert vaultcache.restore(build2, vault, "t", "url") == 2   # vault -> build


def test_render_plain_reattaches_wrapped_quote():
    # "> lists,..." is a wrap continuation of the deeper "> > ...some" line;
    # it must NOT render as its own shallower <pre> outside the blockquote.
    body = ("> > a default file, plus some\n"
            "> wildcards as well\n"
            "> > more text\n")
    out = render_body.render_plain(body)
    assert "<pre>wildcards as well</pre>" not in out


# --- #4 fetch_attachments SSRF hardening -------------------------------------

def test_fetch_attachments_rejects_unsafe_urls():
    import fetch_attachments
    for bad in ("http://lists.xymon.com/x",            # not https
                "https://evil.com/x",                  # wrong host
                "https://lists.xymon.com.evil.com/x",  # suffix trick
                "ftp://lists.xymon.com/x"):            # wrong scheme
        raised = False
        try:
            fetch_attachments.httpget(bad)
        except ValueError:
            raised = True
        assert raised, bad


# --- mailstore.iter_mbox: the one mbox splitter --------------------------------

def test_iter_mbox_unescapes_mboxrd_but_keeps_chunk_verbatim():
    raw = (b"From a@b Mon Jan  1 00:00:00 2024\n"
           b"Message-Id: <1@x>\n\n"
           b"body\n"
           b">From the export it was escaped\n"
           b">>From quoted text keeps one marker\n"
           b"\n"
           b"From c@d Mon Jan  1 00:00:01 2024\n"
           b"Message-Id: <2@x>\n\nsecond\n")
    out = list(mailstore.iter_mbox(raw))
    assert len(out) == 2
    chunk, msg = out[0]
    body = msg.get_payload()
    # one ">" peeled (mboxrd): escaped line restored, quoted line keeps one
    assert "\nFrom the export it was escaped\n" in body
    assert "\n>From quoted text keeps one marker\n" in body
    assert b">From the export" in chunk          # chunk stays byte-verbatim
    assert out[1][1]["Message-Id"] == "<2@x>"


def test_iter_mbox_quoted_from_inside_body_is_not_a_separator():
    raw = (b"From a@b Mon Jan  1 00:00:00 2024\n"
           b"Message-Id: <1@x>\n\n"
           b"He said:\nFrom Mon Jan  1 12:00:00 2024 onwards it broke\n")
    out = list(mailstore.iter_mbox(raw))
    assert len(out) == 1                  # no blank line -> not a separator


# --- webfetch: the one shared hardened HTTP layer ------------------------------

def test_webfetch_allowlist_rejects_unsafe_urls():
    import webfetch
    for bad in ("http://lists.xymon.com/x",            # not https
                "https://evil.com/x",                  # wrong host
                "https://lists.xymon.com.evil.com/x",  # suffix trick
                "ftp://lists.xymon.com/x"):            # wrong scheme
        raised = False
        try:
            webfetch.get(bad, max_bytes=1, allowed_hosts={"lists.xymon.com"})
        except ValueError:
            raised = True
        assert raised, bad


def test_stable_id_is_the_one_permalink_hash():
    import hashlib as _h
    mid = "<abc@example.org>"
    # thread/<tid> (12) and msg/<id> (16) both derive from threads.stable_id
    assert threads.stable_id(mid, 12) == threads._tid(mid)
    assert threads.stable_id(mid, 16) == _h.sha1(
        mid.encode()).hexdigest()[:16]


def test_stable_id_survives_unencodable_msgid():
    # a lone surrogate would crash a strict .encode(); the shared helper must
    # yield a stable id instead of aborting the rebuild
    weird = "<a\udcff@x>"
    assert len(threads.stable_id(weird, 12)) == 12
    assert threads.stable_id(weird, 12) == threads.stable_id(weird, 12)


def test_obfuscate_bounded_gunzip_contract():
    import gzip as _gz

    import obfuscate
    blob = _gz.compress(b"y" * 5000)
    assert obfuscate._bounded_gunzip(blob, 5000) == b"y" * 5000
    assert obfuscate._bounded_gunzip(blob, 4999) is None     # over limit
    assert obfuscate._bounded_gunzip(b"not gzip", 100) is None
    assert obfuscate._bounded_gunzip(blob, -1) is None


def test_webfetch_gunzip_bounded():
    import gzip as _gz

    import webfetch
    blob = _gz.compress(b"x" * 10000)
    assert webfetch.gunzip_bounded(blob, 10000) == b"x" * 10000
    raised = False
    try:
        webfetch.gunzip_bounded(blob, 9999)    # bomb guard: limit enforced
    except ValueError:
        raised = True
    assert raised


# --- #5 cached scrubbed HTML is re-sanitized from raw_html --------------------

def test_scrubbed_html_resanitizes_from_cache(tmp_path):
    import fetch_scrubbed_html as fsh
    url = ("https://lists.xymon.com/xymon/attachments/"
           "20200101/abc/attachment.html")
    db = tmp_path / "a.db"
    conn = mailstore.connect(str(db))
    conn.execute(
        "INSERT INTO message (msgid, month, body) VALUES (?,?,?)",
        ("m1", "2020-01",
         f"-------------- next part --------------\n... scrubbed ...\nURL: <{url}>\n"))
    conn.commit()
    conn.close()
    cache = tmp_path / "scrubbed_html.db"
    cc = sqlite3.connect(str(cache))
    cc.execute("CREATE TABLE html (url TEXT PRIMARY KEY, body_html TEXT, "
               "raw_html TEXT, raw_bytes BLOB)")
    cc.execute("INSERT INTO html (url, body_html, raw_html) VALUES (?,?,?)",
               (url, "STALE-CACHED", "<p>hi</p><script>evil()</script>"))
    cc.commit()
    cc.close()
    fsh.main(["--db", str(db), "--cache", str(cache), "--no-network"])
    conn = mailstore.connect(str(db))
    got = conn.execute(
        "SELECT body_html FROM message WHERE msgid='m1'").fetchone()[0]
    conn.close()
    assert got and "STALE-CACHED" not in got and "<script" not in got.lower()
    assert "hi" in got


# --- dbhash: the republication fingerprint ------------------------------------

def _hash_rows(rows):
    """rows: (month, msgid, subject, body, source) -> dbhash.fingerprint."""
    import os
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "h.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, month, msgid, "
                "in_reply_to, subject, from_name, from_email, date_iso, body, "
                "source, body_html)")
    con.executemany("INSERT INTO message (month, msgid, subject, body, source) "
                    "VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return dbhash.fingerprint(p)


def test_dbhash_order_independent():
    a = _hash_rows([("m", "<1>", "s1", "b1", "list"),
                    ("m", "<2>", "s2", "b2", "list")])
    b = _hash_rows([("m", "<2>", "s2", "b2", "list"),
                    ("m", "<1>", "s1", "b1", "list")])
    assert a == b


def test_dbhash_covers_raw():
    # `raw` is published (archive.db.gz + the downloadable mbox) and obfuscate
    # rewrites it, so a change to raw alone MUST move the fingerprint -- otherwise
    # a privacy scrub that only touches raw never triggers a republish.
    import os
    import tempfile

    def h(raw_value):
        p = os.path.join(tempfile.mkdtemp(), "r.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, month, msgid, "
                    "in_reply_to, subject, from_name, from_email, date_iso, body, "
                    "source, body_html, raw)")
        con.execute("INSERT INTO message (month, msgid, body, raw) "
                    "VALUES ('m','<1>','b',?)", (raw_value,))
        con.commit()
        con.close()
        return dbhash.fingerprint(p)

    assert "raw" in dbhash.COLS
    assert h(b"leak: real@acme.com") != h(b"leak: user-x@xymon.invalid")


def test_dbhash_no_xor_cancellation():
    # Two byte-identical rows must NOT cancel. NULL msgids are allowed to repeat
    # (no UNIQUE covers them), so a duplicated pair is reachable; under the old
    # XOR accumulator the pair cancels to zero and collides with `base`.
    base = [("m", "<1>", "s1", "b1", "list")]
    dup = base + [("m", None, "d", "x", "list"), ("m", None, "d", "x", "list")]
    assert _hash_rows(base) != _hash_rows(dup)


def test_dbhash_none_distinct_from_string_none():
    assert _hash_rows([("m", None, "s", "b", "list")]) != \
        _hash_rows([("m", "None", "s", "b", "list")])


def test_dbhash_detects_content_change():
    assert _hash_rows([("m", "<1>", "s", "body one", "list")]) != \
        _hash_rows([("m", "<1>", "s", "body two", "list")])


# --- gh_discussion_rows: GitHub bodyHTML is sanitized at ingest ---------------

def test_gh_discussion_sanitizes_bodyhtml():
    disc = {
        "id": "D1", "title": "T", "createdAt": "2024-01-02T03:04:05Z",
        "author": {"login": "alice"}, "body": "x",
        "bodyHTML": "<p>hi</p><script>alert(1)</script>",
        "comments": {"nodes": []},
    }
    bh = mailstore.gh_discussion_rows(disc)[0]["body_html"]
    assert "<script" not in bh.lower() and "alert(1)" not in bh
    assert "hi" in bh
