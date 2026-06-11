#!/usr/bin/env python3
"""Generate a static mirror of the Xymon archive from SQLite.

Layout produced under ``site/``::

    index.html               years -> months with counts
    <YYYY-Month>.html        one month, messages by date (the only month view)
    thread/<tid>.html        one thread, all messages (a message's permalink is
                             thread/<tid>.html#m-<id>; there are no per-msg pages)

This module is the orchestrator (build()) plus the index-tab and thread-page
writers. The page chrome and HTML primitives live in pagelib.py; the search
subsystem in search_index.py; the month pages in month_pages.py.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import pagelib
import threads
from fold import STATS as fold_stats, fold_thread
from mailstore import MONTH_ORDER, month_key
from month_pages import _write_month_pages
from pagelib import (  # page chrome + HTML primitives
    _TABS, _bar_row, _github_base, _human, _load_names, _meta_desc,
    _not_found_page, _safe, _write_sitemaps, e, msg_name, page, whom,
    CSS, SCRIPT, _CSS_NAME, _FAVICON, _JS_NAME)
from render_body import body_to_html  # body -> HTML rendering subsystem
from search_index import (
    _SEARCH_BLURB, SEARCH_BODY, _search_grouping, _write_search_indexes)


_ARCHIVE_JS = """
<script>
function loadFrag(panel, frag){
  // shared injection pipeline (script.js); no `sel` -> raw fragment mode.
  // Deferred script.js is loaded by the time a click can land here.
  window.inject(panel, 'frag/' + encodeURIComponent(frag) + '.html', {});
}
document.querySelectorAll('.years .mgrid a[data-m]').forEach(function(a){
  a.addEventListener('click', function(e){
    e.preventDefault();
    var grid = a.parentElement, panel = grid.nextElementSibling;
    var open = grid.querySelector('.mgactive');
    if (open === a && !panel.hidden){ panel.hidden = true; a.classList.remove('mgactive'); return; }
    if (open) open.classList.remove('mgactive');
    a.classList.add('mgactive');
    panel.hidden = false; loadFrag(panel, a.dataset.m);  // messages by date
  });
});
</script>
"""

_THREADS_PAGE = """
<p id=tstat class=meta>Loading threads&#8230;</p>
<ul id=tlist class=recent></ul>
<p><button id=tmore class=tbtn hidden>Show more threads</button></p>
<script>
fetch('search-index.json').then(function(r){return r.json();}).then(function(D){
  var th={};
  for(var i=0;i<D.length;i++){var t=D[i][5]; (th[t]||(th[t]=[])).push(i);}
  var G=[];
  for(var k in th){var idx=th[k];
    idx.sort(function(a,b){var x=D[a][3]||'',y=D[b][3]||'';return x<y?-1:(x>y?1:0);});
    G.push(idx);}
  G.sort(function(a,b){var x=D[a[a.length-1]][3]||'',y=D[b[b.length-1]][3]||'';
    return x<y?1:(x>y?-1:0);});
  document.getElementById('tstat').textContent=G.length.toLocaleString()+' threads';
  var shown=0,B=50,list=document.getElementById('tlist'),more=document.getElementById('tmore');
  function addThread(idx){
    // the shared site-wide list row (script.js lineEl); expansion is handled
    // by the global .xpand dispatcher (data-tid -> whole thread inline).
    var f=D[idx[0]],last=(D[idx[idx.length-1]][3]||'').slice(0,10),n=idx.length,tid=f[6];
    var att=0; for(var j=0;j<idx.length;j++) att+=D[idx[j]][4]||0;
    var fb=tid?'thread/'+tid+'.html':'msg/'+f[0]+'.html';
    list.appendChild(lineEl({subject:f[1],author:f[2],when:last,count:n,
      att:att,tid:tid||null,mid:tid?null:f[0],href:fb,
      threadHref:tid?fb:null}));
  }
  function batch(){
    // lineEl lives in the deferred script.js; retry briefly if the index
    // fetch won the race against it.
    if(typeof lineEl!=='function'){setTimeout(batch,40);return;}
    var end=Math.min(shown+B,G.length);
    for(;shown<end;shown++) addThread(G[shown]);
    more.hidden=shown>=G.length;
  }
  more.addEventListener('click',batch);batch();
}).catch(function(){document.getElementById('tstat').textContent='Could not load the thread index \\u2014 check the connection and reload.';});
</script>
"""


# Folded into every message-page signature: bump when the RENDERING changes
# (not the data), so the incremental manifest re-renders all pages once.
RENDER_VERSION = "21-furniture-scraps"


_CID_IMG = re.compile(r'<img src="cid:([^"]+)"[^>]*>')


def _att_cid(a):
    return a["cid"] if "cid" in a.keys() else None


def _resolve_cids(mbody: str, atts) -> str:
    """Point the sanitizer's <img src="cid:..."> placeholders at the message's
    extracted attachment files (metadata-stripped, content-addressed,
    deduped). A reference whose part was filtered at ingest (signature logo,
    over-cap) is dropped entirely rather than left broken. Both the stored
    cid and the one inside body_html went through the same obfuscation
    transform, so matching survives address-shaped Content-IDs."""
    if "cid:" not in mbody:
        return mbody
    cmap = {}
    for a in atts:
        c = _att_cid(a)
        if c:
            cmap[c] = a
            cmap.setdefault(c.lower(), a)

    def _sub(m):
        key = html.unescape(m.group(1))
        a = cmap.get(key) or cmap.get(key.lower())
        if not a:
            return ""
        fname = _safe(a["filename"])
        return (f"<img src='../att/{_att_dir(a)}/{e(fname)}' "
                f"alt='{e(fname)}' loading=lazy>")
    return _CID_IMG.sub(_sub, mbody)


def _att_dir(a) -> str:
    """Content-addressed att/ directory: identical bytes are stored once on
    the site however many messages carry them, and the URL is stable across
    DB rebuilds (row ids churn; content hashes don't)."""
    return hashlib.sha1(a["content"] or b"").hexdigest()[:16]


def _sweep_att(out: Path, atts_by_msgid) -> None:
    """Delete att/ entries not derived from the current DB. Content-addressed
    dirs are immutable, so a later-cleaned/withheld attachment lands in a NEW
    dir -- without this sweep the OLD bytes would linger in the CI-cached
    site. Also clears orphans of deleted rows and the pre-hash numeric dirs
    (neither was ever pruned before). The expected set spans ALL attachments,
    independent of the incremental thread skip-logic, so cached files of
    skipped threads survive."""
    expected: dict[str, set] = {}
    for atts in atts_by_msgid.values():
        for a in atts:
            expected.setdefault(_att_dir(a), set()).add(_safe(a["filename"]))
    root = out / "att"
    if not root.exists():
        return
    for d in root.iterdir():
        if d.name not in expected:
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        else:
            for f in d.iterdir():
                if f.name not in expected[d.name]:
                    f.unlink()


# A generated per-month artifact: "2024-January.html", "2024-January.txt.gz",
# or the undated "unknown.*" bucket. Deliberately strict so the root's
# index*.html / 404.html / sitemap.xml / robots.txt / hashed assets never match.
_MONTH_FILE = re.compile(r"^(\d{4}-[A-Za-z]+|unknown)\.(?:html|txt\.gz)$")


def _sweep_pages(out: Path, conn, months) -> None:
    """Delete generated pages whose source rows/months are gone from the DB.

    The incremental render rewrites only changed threads and never removes the
    msg/, frag/, month or mbox files of deleted messages, so they linger in the
    CI-cached site and ship as ghost pages (a deleted message stayed reachable
    at its old msg/<id>.html). Like _sweep_att, the expected set spans the WHOLE
    DB, independent of the incremental skip, so cached files of skipped threads
    survive while orphans are pruned."""
    expected_msg = {msg_name(r)
                    for r in conn.execute("SELECT id, msgid FROM message")}
    msgdir = out / "msg"
    if msgdir.exists():
        for f in msgdir.glob("*.html"):
            if f.stem not in expected_msg:
                f.unlink()
    monthset = set(months)
    fragdir = out / "frag"
    if fragdir.exists():
        for f in fragdir.glob("*.html"):
            if f.stem not in monthset:
                f.unlink()
    for f in out.iterdir():           # root month pages + downloadable mbox
        if f.is_file():
            m = _MONTH_FILE.match(f.name)
            if m and m.group(1) not in monthset:
                f.unlink()


def build(db: Path, out: Path, base_url: str = "") -> None:
    """Render the whole site: a thin orchestrator over the phase functions
    below (load -> assets -> index tabs -> search indexes -> months ->
    threads -> SEO), each independently testable against a fixture DB."""
    pagelib._BASE = (base_url or "").rstrip("/")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    has_tid = "thread_id" in {r[1] for r in
                              conn.execute("PRAGMA table_info(message)")}
    _load_names(conn)
    _prepare_out(out)
    atts_by_msgid, att_counts, att_msg_per_month = _load_attachments(conn)

    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT month FROM message")]
    months.sort(key=month_key, reverse=True)
    counts = {m: conn.execute(
        "SELECT COUNT(*) FROM message WHERE month=?", (m,)).fetchone()[0]
        for m in months}
    years: dict[str, list[str]] = {}
    for m in months:
        years.setdefault(m.split("-", 1)[0], []).append(m)
    total = sum(counts.values())
    yrs = sorted(years)
    span = f"{yrs[0]}–{yrs[-1]}" if yrs else ""

    _write_index_tabs(conn, out, counts, years, total, span,
                      att_msg_per_month)
    tid_of, htid_of = _search_grouping(conn)
    sidx = _write_search_indexes(conn, out, has_tid, att_counts, tid_of,
                                 htid_of)
    _write_month_pages(conn, out, months, att_counts, has_tid, htid_of)
    bythread = _write_thread_pages(conn, out, has_tid, atts_by_msgid, htid_of)
    _sweep_att(out, atts_by_msgid)
    _sweep_pages(out, conn, months)
    _write_seo(out, months, bythread, sidx)
    conn.close()
    print(f"Generated site in {out}/ ({len(months)} months)")


def _prepare_out(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "msg").mkdir(exist_ok=True)   # canonical single-message permalink pages
    (out / "att").mkdir(exist_ok=True)
    (out / "frag").mkdir(exist_ok=True)
    (out / _CSS_NAME).write_text(CSS, "utf-8")       # content-hashed names so the
    (out / _JS_NAME).write_text(SCRIPT, "utf-8")     #   browser/CDN never serve a
    #   stale asset; the <link>/<script> hrefs carry the same hash (see page()).
    for old in out.glob("style.*.css"):              # drop superseded hashed assets
        if old.name != _CSS_NAME:
            old.unlink()
    for old in out.glob("script.*.js"):
        if old.name != _JS_NAME:
            old.unlink()
    for bare in ("style.css", "script.js"):          # drop pre-hash leftovers (old cache)
        if (out / bare).exists():
            (out / bare).unlink()
    # drop the obsolete per-month sort variants (the date/threaded/author switcher
    # was removed -- only {m}.html / frag/{m}.html remain). Stale files survive in
    # the cache-restored site/, so prune them so they don't linger on the deploy.
    for sub in (out, out / "frag"):
        for pat in ("*-date.html", "*-author.html", "*-thread.html"):
            for old in sub.glob(pat):
                old.unlink()


def _load_attachments(conn):
    """Attachments grouped by their message (msgid):
    (atts_by_msgid, att_counts, att_msg_per_month)."""
    acols = {r[1] for r in conn.execute("PRAGMA table_info(attachment)")}
    _ccol = ", cid" if "cid" in acols else ""
    atts_by_msgid: dict[str, list] = defaultdict(list)
    for a in conn.execute(
            "SELECT id, msgid, filename, content_type, size, content"
            f"{_ccol} FROM attachment WHERE msgid IS NOT NULL"):
        atts_by_msgid[a["msgid"]].append(a)
    att_counts = {mid: len(v) for mid, v in atts_by_msgid.items()}
    att_msg_per_month = dict(conn.execute(
        "SELECT month, COUNT(DISTINCT msgid) FROM attachment "
        "WHERE msgid IS NOT NULL GROUP BY month"))
    return atts_by_msgid, att_counts, att_msg_per_month


def _write_index_tabs(conn, out: Path, counts, years, total, span,
                      att_msg_per_month) -> None:
    """The four tab pages: Search (hero + widget), Threads, Archive, Stats."""
    # --- browse-by-year grid (anchored so the activity bars can link to it)
    # classic Pipermail-style calendar: every year shows all 12 months in a
    # 4-per-row grid; months with mail are links (count on hover), rest greyed.
    month_names = sorted(MONTH_ORDER, key=MONTH_ORDER.get)
    grid = "<div class=years>"
    for year in sorted(years, reverse=True):
        present = {mm.split("-", 1)[1]: mm for mm in years[year]}
        grid += f"<h3 id=y{year}>{e(year)}</h3><div class=mgrid>"
        for name in month_names:
            if name in present:
                mm = present[name]
                am = att_msg_per_month.get(mm, 0)
                tip = f"{counts[mm]} messages" + (
                    f", {am} with attachments" if am else "")
                grid += (f"<a href='{e(mm)}.html' data-m='{e(mm)}' "
                         f"title='{tip}'>{e(name)}</a>")
            else:
                grid += f"<span>{e(name)}</span>"
        grid += "</div><div class=mpanel hidden></div>"
    grid += "</div>" + _ARCHIVE_JS

    # --- activity-by-year bars
    yr_count = {y: sum(counts[m] for m in mm) for y, mm in years.items()}
    maxy = max(yr_count.values()) if yr_count else 1
    bars = "<h2 class=stath>Messages per year</h2><div class=bars>"
    for y in sorted(years, reverse=True):
        # the id=y<year> anchors live on the Archive tab (index-year.html)
        bars += _bar_row(
            "bar", f"<a class=byr href='index-year.html#y{y}'>{e(y)}</a>",
            yr_count[y], maxy)
    bars += "</div>"

    # --- most active participants (top 15 by displayed author name; multi-
    # address people merge because the name is already resolved in from_name)
    ucount = Counter()
    for r in conn.execute("SELECT from_name, from_email FROM message"):
        nm = whom(r)
        if nm and "@" not in nm and nm != "(unknown)":
            ucount[nm] += 1
    top_u = ucount.most_common(15)
    maxu = top_u[0][1] if top_u else 1
    bars += "<h2 class=stath>Most active participants</h2><div class=bars>"
    for nm, c in top_u:
        bars += _bar_row("ubar", f"<span class=un>{e(nm)}</span>", c, maxu)
    bars += "</div>"

    # Threads tab: a client-side browser over search-index.json (already loaded
    # by Search, so no extra payload). It groups every message by its thread id
    # -- the same reply+subject grouping the index uses -- and lists ALL threads
    # newest-first, rendered in batches via "Show more threads".
    recent = _THREADS_PAGE

    hero = (f"<div class=hero><h1>Xymon Mailing List Archive</h1>"
            f"<p class=tag>Two decades of the Xymon &amp; Hobbit community, "
            f"searchable in your browser.</p>"
            f"<p class=meta><b>{total:,}</b> messages &middot; <b>{span}</b>"
            f" &middot; addresses pseudonymised</p></div>")
    # single-section layouts (hero shared); the tab label IS the section name,
    # so there is no separate heading. Search box lives only on the Search tab.
    sections = {"index.html": "", "index-latest.html": recent,
                "index-year.html": grid, "index-dashboard.html": bars}

    def tabs(current):
        parts = [(f"<b>{lbl}</b>" if lbl == current
                  else f"<a href='{href}'>{lbl}</a>")
                 for lbl, href in _TABS]
        return "<p class=altlinks>" + " | ".join(parts) + "</p>"

    # the full search widget, minus its own <h1> and the descriptive blurb --
    # the hero already sets the scene, so the box sits clean right under it.
    widget = (SEARCH_BODY
              .replace("<h1>Search the archive</h1>", "")
              .replace(_SEARCH_BLURB, "")
              .replace("__N__", str(total)))

    _tab_desc = {
        "index.html": f"Searchable archive of the Xymon (Hobbit) monitoring "
                      f"mailing list — {total:,} messages, {span}, "
                      f"addresses pseudonymised.",
        "index-latest.html": "All threads of the Xymon mailing list archive, "
                             "newest first.",
        "index-year.html": "Browse the Xymon mailing list archive by year "
                           "and month.",
        "index-dashboard.html": "Activity statistics for the Xymon mailing "
                                "list archive.",
    }
    for lbl, href in _TABS:
        # the search box lives only on the Search tab, not on Archive / Stats
        mid = widget if href == "index.html" else ""
        (out / href).write_text(
            page("Xymon Archive", hero + tabs(lbl) + mid + sections[href],
                 header=False, desc=_tab_desc.get(href),
                 canon="" if href == "index.html" else href), "utf-8")


def _read_manifest(out: Path):
    """(manifest_path, old_threads, incremental) for the thread pass.

    Incremental change detection: the ~tens-of-thousands of per-message pages
    dominate the build. With INCREMENTAL=1 and a previous site/.manifest.json
    (restored from CI cache), only pages whose input changed are re-rendered;
    the rest are kept from the cached site. The manifest is keyed by THREAD:
    a thread re-renders when any member's content (or RENDER_VERSION)
    changes. No manifest / INCREMENTAL unset -> full rebuild."""
    manifest_path = out / ".manifest.json"
    old_manifest: dict = {}
    if manifest_path.exists():
        try:
            old_manifest = json.loads(manifest_path.read_text("utf-8"))
        except Exception:                       # corrupt cache -> full rebuild
            old_manifest = {}
    old_threads = old_manifest.get("threads", {})
    # If the hashed asset names changed (JS/CSS edited), every page must be
    # re-rendered so its <link>/<script> points at the new file -- otherwise
    # un-rendered pages would 404 on the now-deleted old asset. So a CSS/JS change
    # forces a full (non-incremental) build for that run.
    assets_changed = old_manifest.get("assets") != [_JS_NAME, _CSS_NAME]
    incremental = (os.environ.get("INCREMENTAL") == "1"
                   and bool(old_threads) and not assets_changed)
    return manifest_path, old_threads, incremental


def _write_thread_pages(conn, out: Path, has_tid, atts_by_msgid,
                        htid_of=None) -> dict:
    """Thread pages: ONE page per thread, every message in order, each a
    collapsible <details> block anchored #m-<id> (that anchor IS the message
    permalink); the canonical msg/ pages are written alongside. Carries the
    full body, author/date/source and the message's attachments. Incremental:
    a thread re-renders only when its content signature (or RENDER_VERSION)
    changes. Returns the thread map (its keys feed the sitemap)."""
    manifest_path, old_threads, incremental = _read_manifest(out)
    (out / "thread").mkdir(exist_ok=True)
    bythread: dict = defaultdict(list)
    for r in conn.execute("SELECT * FROM message").fetchall():
        if has_tid and r["thread_id"]:
            key = r["thread_id"]                # production: enriched thread id
        elif not has_tid and htid_of:
            key = htid_of.get(r["id"]) or msg_name(r)   # standalone grouping
        else:
            key = msg_name(r)                   # no grouping signal: own page
        bythread[key].append(r)

    def _att_block(r, seen=None):
        """Write this message's attachment files and return the HTML box.
        With ``seen`` (a thread-scoped set of content digests), an attachment
        whose CONTENT already appeared on an earlier message is omitted from
        the box: mail clients re-attach the quoted message's inline images
        under generic names (image001.png ...), so the same screenshot showed
        up once per reply -- duplicated content folds away on the thread page
        exactly like quoted text. The files themselves are still written (the
        canonical msg/ pages keep every message's faithful full list)."""
        atts = atts_by_msgid.get(r["msgid"], ())
        if not atts:
            return ""
        links, listed = "", 0
        for a in atts:
            fname = _safe(a["filename"])
            digest = _att_dir(a)
            adir = out / "att" / digest
            adir.mkdir(parents=True, exist_ok=True)
            (adir / fname).write_bytes(a["content"])
            if seen is not None:
                if digest in seen:
                    continue                    # a re-attachment of earlier content
                seen.add(digest)
            listed += 1
            href = f"../att/{digest}/{e(fname)}"
            is_img = ((a["content_type"] or "").lower().startswith("image/")
                      or fname.lower().endswith((".png", ".jpg", ".jpeg")))
            # screenshots render inline (metadata already stripped by
            # obfuscate.py); everything else stays a download link. A
            # cid-referenced image already shows at its in-text position
            # (see _resolve_cids), so its box entry stays a plain link.
            label = (f"<img src='{href}' alt='{e(fname)}' loading=lazy>"
                     f"{e(fname)}" if is_img and not _att_cid(a) else e(fname))
            links += (f"<li><a href='{href}'>{label}</a> "
                      f"<span class=meta>{e(a['content_type'] or '')} &middot; "
                      f"{_human(a['size'])}</span></li>")
        if not listed:
            return ""
        return (f"<div class=att><b>Attachments ({listed})</b>"
                f"<ul>{links}</ul></div>")

    new_threads, nth = {}, 0
    for tid, members in bythread.items():
        members.sort(key=threads.order)        # the one shared chronology
        sig = hashlib.blake2b(("\x00".join(
            [RENDER_VERSION] +
            ["\x1f".join([msg_name(r), r["subject"] or "", whom(r),
                          r["from_email"] or "", r["date_raw"] or "",
                          r["source"] or "", r["body"] or "", r["body_html"] or "",
                          ";".join(
                              f"{a['id']}:{a['size']}:{_att_cid(a) or ''}:"
                              f"{_att_dir(a)}:{_safe(a['filename'])}:"
                              f"{a['content_type'] or ''}"
                              for a in atts_by_msgid.get(r["msgid"], ()))])
             for r in members])).encode("utf-8", "replace"),
            digest_size=16).hexdigest()
        new_threads[tid] = sig
        if (incremental and old_threads.get(tid) == sig
                and (out / "thread" / f"{tid}.html").exists()):
            continue
        head = members[0]
        blocks = []
        mbodies = [_resolve_cids(body_to_html(r["body"], r["body_html"]),
                                 atts_by_msgid.get(r["msgid"], ()))
                   for r in members]
        # server-side quote folding (fold.py): each message's content-proven
        # quoted tail collapses behind a toggle ON THE THREAD PAGE; the
        # canonical msg/ pages keep the unfolded body (stable reference).
        folded = fold_thread(mbodies, [whom(r) for r in members])
        seen_att: set = set()      # content digests already shown in this thread
        for r, mbody, fbody in zip(members, mbodies, folded):
            anchor = msg_name(r)
            src = r["source"] or "list"
            badge = f"<span class='badge {e(src)}'>{e(src)}</span>"
            email = (f" &lt;{e(r['from_email'])}&gt;"
                     if (r["from_email"] and "@" in r["from_email"]
                         and not r["from_email"].endswith("@xymon.invalid"))
                     else "")
            tatts = _att_block(r, seen_att)    # thread box: content-deduped
            matts = _att_block(r)              # msg page: faithful full list
            # thread block (foldable, threaded view). The copy marker's data-href
            # is the MESSAGE permalink -> its canonical msg/<id>.html page.
            blocks.append(
                f"<details class=tmsg id=m-{anchor} open>"
                f"<summary>{badge} <b>{e(whom(r))}</b>{email} "
                f"<span class=meta>&middot; {e(r['date_raw'])} &middot; "
                f"<button class=copy type=button data-href='msg/{anchor}.html'"
                f" title='copy link to this message'>&#128279; link</button>"
                f"</span></summary>{fbody}{tatts}</details>")
            # canonical single-message page: full body, quotes NOT folded, no JS
            # (scripts=False) -> a stable, feature-free reference for permalinks.
            msg_html = (
                f"<div class=msg><h1>{e(r['subject']) or '(no subject)'}</h1>"
                f"<p class=meta>{badge} <b>{e(whom(r))}</b>{email}"
                f"<br>{e(r['date_raw'])}"
                f"{'<br>Message-Id: ' + e(r['msgid']) if r['msgid'] else ''}</p>"
                f"{mbody}{matts}</div>")
            (out / "msg" / f"{anchor}.html").write_text(
                page(r["subject"] or "message", msg_html, root="../",
                     scripts=False, desc=_meta_desc(r),
                     canon=f"msg/{anchor}.html"), "utf-8")
        tbody = (f"<div class=thread>"
                 f"<h1>{e(head['subject']) or '(no subject)'} "
                 f"<button class=copy type=button data-href='thread/{e(tid)}.html'"
                 f" title='copy link to this thread'>&#128279; link</button></h1>"
                 f"<p class=meta>{len(members)} message"
                 f"{'s' if len(members) != 1 else ''} in this thread</p>"
                 + "".join(blocks) + "</div>")
        (out / "thread" / f"{tid}.html").write_text(
            page(head["subject"] or "thread", tbody, root="../",
                 desc=f"Xymon mailing list thread, {len(members)} message"
                      f"{'s' if len(members) != 1 else ''}, "
                      f"{(head['date_iso'] or '')[:10]}.",
                 canon=f"thread/{tid}.html"), "utf-8")
        nth += 1
    print(f"{'incremental' if incremental else 'full'} render: "
          f"{nth}/{len(bythread)} thread pages")
    if fold_stats["errors"]:
        print(f"WARNING: {fold_stats['errors']} message(s) rendered unfolded "
              f"after a fold error (see stderr)")

    # drop pages for threads that no longer exist, then persist the manifest.
    for gone in set(old_threads) - set(new_threads):
        stale = out / "thread" / f"{gone}.html"
        if stale.exists():
            stale.unlink()
    manifest_path.write_text(
        json.dumps({"threads": new_threads, "assets": [_JS_NAME, _CSS_NAME]},
                   separators=(",", ":")), "utf-8")
    return bythread


def _write_seo(out: Path, months, bythread, sidx) -> None:
    """Discoverability scaffolding: favicon, custom 404, robots, sitemap.
    The sitemap and robots' Sitemap line need absolute URLs, so they appear
    only when a base URL is known (CI); the rest is emitted unconditionally."""
    base = pagelib._BASE
    (out / "favicon.svg").write_text(_FAVICON, "utf-8")
    (out / "404.html").write_text(_not_found_page(), "utf-8")
    _prefix = urlsplit(base).path if base else ""
    robots = f"User-agent: *\nDisallow: {_prefix}/frag/\n"
    if base:
        robots += f"Sitemap: {base}/sitemap.xml\n"
    (out / "robots.txt").write_text(robots, "utf-8")
    if base:
        _write_sitemaps(out, (
            [""] + [h for _, h in _TABS if h != "index.html"]
            + [f"{m}.html" for m in months]
            + [f"thread/{t}.html" for t in sorted(bythread)]
            + [f"msg/{row[0]}.html" for row in sidx]))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate static Xymon mirror")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--out", default="site", type=Path)
    ap.add_argument("--base-url",
                    default=os.environ.get("BASE_URL") or _github_base(),
                    help="absolute site URL; enables sitemap.xml + canonical "
                         "tags (defaults to $BASE_URL, or the GitHub Pages "
                         "URL when building in Actions)")
    args = ap.parse_args()
    build(args.db, args.out, args.base_url)


if __name__ == "__main__":
    main()
