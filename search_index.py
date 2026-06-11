#!/usr/bin/env python3
"""Client-side search subsystem.

SEARCH_BODY is the search page/widget template (generate's index tabs embed
it); _search_grouping and _write_search_indexes build the thread grouping and
the search-index.json / body-index.json.gz payloads that template consumes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

import threads
from pagelib import msg_name, whom
from render_body import _QUOTE_PREFIX, strip_footer, strip_scrub_notes

_SEARCH_BLURB = """<p class=meta>Search subject, sender and full message text
across __N__ messages.</p>"""

SEARCH_BODY = """
<h1>Search the archive</h1>
""" + _SEARCH_BLURB + """
<input id=q type=search placeholder="e.g. SSL certificate, or a sender name"
       aria-label="Search the archive" autofocus autocomplete=off>
<p class=meta>
  <label><input type=checkbox id=attonly> &#128206; with attachments only</label>
</p>
<p id=stat class=meta>Loading search index&#8230;</p>
<ul id=res></ul>
<script>
let DATA=[], BODIES=null;
let bodyReq=null, note='';
const q=document.getElementById('q'), res=document.getElementById('res'),
      stat=document.getElementById('stat'),
      attonly=document.getElementById('attonly');
const canDeep=typeof DecompressionStream!=='undefined';
if(!canDeep) note=' \\u00b7 full-text search needs a newer browser '+
  '\\u2014 matching subject & sender only';
// The big message-text index loads only once a search actually happens, so
// just visiting the page costs the small subject/sender index alone.
function loadBodies(){
  if(bodyReq||!canDeep) return;
  note=' \\u00b7 loading full message text\\u2026';
  bodyReq=fetch('body-index.json.gz').then(r=>{
    if(!r.ok) throw new Error('HTTP '+r.status);
    return new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).json();
  }).then(b=>{BODIES=b; note=''; run();})
   .catch(()=>{note=' \\u00b7 message text failed to load '+
     '\\u2014 matching subject & sender only'; run();});
}
fetch('search-index.json').then(r=>{
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}).then(d=>{DATA=d;
  stat.textContent=d.length+' messages indexed.'+note;
  const p=new URLSearchParams(location.search);   // topic chips land here
  if(p.has('q')) q.value=p.get('q');
  if(p.get('att')==='1') attonly.checked=true;
  run();})
 .catch(()=>{stat.textContent=
   'Could not load the search index \\u2014 check the connection and reload.';});

function hl(text, terms){   // bold the matched terms -- one scanner for the
  // whole site: delegates to hlTerms (script.js), so previews and injected
  // messages can never highlight differently. script.js is deferred; on the
  // rare first paint before it runs the preview is just unhighlighted (the
  // next keystroke re-runs).
  const frag=document.createDocumentFragment();
  frag.appendChild(document.createTextNode(text));
  if(typeof hlTerms==='function') hlTerms(frag,terms);
  return frag;
}
function snippets(text, terms, max){        // up to `max` distinct hit windows
  const low=text.toLowerCase(), pos=[];
  for(const t of terms){let p=low.indexOf(t);
    while(p>=0){pos.push(p); p=low.indexOf(t,p+1);}}
  pos.sort((a,b)=>a-b);
  const out=[], used=[];
  for(const p of pos){
    if(out.length>=max) break;
    if(used.some(q=>Math.abs(q-p)<100)) continue;   // merge nearby occurrences
    used.push(p);
    const a=Math.max(0,p-30), b=Math.min(text.length,p+140);
    out.push((a>0?'\\u2026':'')+text.slice(a,b).replace(/\\s+/g,' ')
             +(b<text.length?'\\u2026':''));
  }
  return out;
}
function run(){
  if(!DATA.length) return;                  // subject index still loading
  // lineEl lives in the deferred script.js; if the index won the race against
  // it (cached fetch during parse), retry shortly instead of half-rendering.
  if(typeof lineEl!=='function'){setTimeout(run,40);return;}
  const terms=q.value.toLowerCase().split(/\\s+/).filter(Boolean);
  const onlyAtt=attonly.checked;
  if(terms.length) loadBodies();            // first real search pulls the bodies
  res.textContent='';
  if(!terms.length && !onlyAtt){
    stat.textContent=DATA.length+' messages indexed.'+note; return;}
  const deep=!!BODIES;
  const hits=[];
  for(let i=0;i<DATA.length;i++){
    const m=DATA[i];
    if(onlyAtt && !m[4]) continue;
    const hay=(m[1]+' '+m[2]+(deep?' '+BODIES[i]:'')).toLowerCase();
    if(terms.every(t=>hay.includes(t))) hits.push(i);
  }
  // flat list, newest first -- one dense line per matching message, date at the
  // end; clicking the line expands that message inline.
  hits.sort((a,b)=>(DATA[b][3]||'').localeCompare(DATA[a][3]||''));
  const CAP=300;
  stat.textContent=hits.length+' match'+(hits.length==1?'':'es')+
    (onlyAtt?' with attachments':'')+
    (hits.length>CAP?' \\u00b7 showing the newest '+CAP:'')+note;
  let shown=0;
  for(const i of hits){
    if(shown>=CAP) break; shown++;
    const m=DATA[i];
    // the shared site-wide list row (script.js); date slice -- the full [3]
    // carries time so same-day results sort chronologically.
    const li=lineEl({subject:m[1],author:m[2],when:(m[3]||'').slice(0,10),
      att:m[4],mid:m[0],href:'msg/'+m[0]+'.html',
      threadHref:m[6]?'thread/'+m[6]+'.html#m-'+m[0]:'msg/'+m[0]+'.html',
      msgHref:'msg/'+m[0]+'.html'});
    // up to 6 preview lines: the text around each match (term highlighted), or
    // the start of the mail when the hit is only in the subject/sender. No
    // preview before the body index has arrived (subject/sender-only phase).
    const prev=document.createElement('div'); prev.className='sprev';
    const sns=(deep&&terms.length)?snippets(BODIES[i],terms,6):[];
    if(sns.length){ for(const sn of sns){ const p=document.createElement('div');
      p.className='pline'; p.appendChild(hl(sn,terms)); prev.appendChild(p); } }
    else if(deep){ const p=document.createElement('div'); p.className='pline';
      p.textContent=(BODIES[i]||'').slice(0,200); prev.appendChild(p); }
    if(prev.childNodes.length) li.appendChild(prev);
    res.appendChild(li);
  }
}
let t; q.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(run,200);});
attonly.addEventListener('change',run);
// test seam (inert in production): ui_test drives the search through this
// instead of patching the script's source text.
window.__searchTest={set:(d,b)=>{DATA=d;BODIES=b;},run:run};
</script>
"""


def _search_grouping(conn):
    """Group messages into threads once (reply links + a date-bounded shared
    subject, threads.components()) and return two views:

      tid_of  -- per-message numeric partition key, numbered in row order, so
                 the client can cluster search hits under their thread ([5]).
      htid_of -- per-message STABLE hex thread id (anchored on the earliest
                 message's Message-Id). A DB without a thread_id column (the
                 standalone public pipeline) uses this so a parent and its
                 reply share one multi-message thread page and link to it;
                 production supplies thread_id and ignores htid_of.

    date_iso is selected because the subject-merge edge is now time-bounded
    (threads.components)."""
    trows = conn.execute(
        "SELECT id, msgid, in_reply_to, subject, date_iso FROM message"
    ).fetchall()
    comps = threads.components(trows)
    group_of = {r["id"]: root for root, members in comps.items()
                for r in members}
    roots, tid_of = {}, {}
    for r in trows:
        tid_of[r["id"]] = roots.setdefault(group_of[r["id"]], len(roots))
    htid_of = {}
    for members in comps.values():
        anchor = min(members, key=threads.order)
        htid = (threads.stable_id(anchor["msgid"], threads._TID_LEN)
                if anchor["msgid"] else f"x{anchor['id']}")
        for r in members:
            htid_of[r["id"]] = htid
    return tid_of, htid_of


def _write_search_indexes(conn, out: Path, has_tid, att_counts,
                          tid_of, htid_of) -> list:
    """Write search-index.json + body-index.json.gz; returns the rows (sidx)
    so the sitemap can enumerate the msg/ pages."""
    # ---- search indexes (dependency-free client side):
    #   search-index.json    small: subject + author + date + att flag + thread
    #   body-index.json.gz   bodies, aligned by row order; loaded for deep search
    # Deep-search index de-dup (see loop): index each run of words once, at its
    # earliest occurrence, so a quoted/forwarded passage is found at its origin
    # rather than in every reply. Process oldest-first so "earliest" is the
    # origin. Shingle hashing is a stable 8-byte blake2b for reproducible builds.
    _SHINGLE = 8
    seen: set = set()

    def _shkey(s):
        return hashlib.blake2b(s.encode("utf-8", "replace"),
                               digest_size=8).digest()

    sidx, bidx = [], []
    _tcol = ", thread_id" if has_tid else ""
    for r in conn.execute(
            "SELECT id, msgid, subject, from_name, from_email, date_iso, body"
            f"{_tcol} FROM message ORDER BY date_iso IS NULL, date_iso, id"):
        sidx.append([
            msg_name(r), r["subject"] or "",
            whom(r),
            # [3] "YYYY-MM-DD HH:MM": lists display the date slice; the full
            # string refines same-day ordering (lexicographic = chronologic)
            (r["date_iso"] or "")[:16].replace("T", " "),
            att_counts.get(r["msgid"], 0),
            tid_of[r["id"]],                       # [5] grouping key (per build)
            r["thread_id"] if has_tid                # [6] stable thread/<tid> link
            else htid_of.get(r["id"], "")])
        b = (r["body"] or "").replace("\r\n", "\n").replace("\r", "\n")
        b = strip_footer(strip_scrub_notes(b))
        # Flatten: drop per-line ">" markers and collapse line breaks, so a quote
        # that was re-wrapped, prefixed with junk, or pasted WITHOUT ">" still
        # matches its origin. De-dup by word shingles: a run of K words already
        # seen anywhere (its origin or an earlier quote) is dropped, so a passage
        # is indexed once regardless of the surrounding wrapping/headers.
        flat = re.sub(r"\s+", " ",
                      " ".join(_QUOTE_PREFIX.sub("", ln)
                               for ln in b.split("\n"))).strip()
        words = flat.split(" ") if flat else []
        low = [w.lower() for w in words]
        keep = bytearray([1]) * len(words)
        for i in range(len(words) - _SHINGLE + 1):
            h = _shkey(" ".join(low[i:i + _SHINGLE]))
            if h in seen:
                for j in range(i, i + _SHINGLE):
                    keep[j] = 0
            else:
                seen.add(h)
        bidx.append(" ".join(w for w, k in zip(words, keep) if k))
    (out / "search-index.json").write_text(
        json.dumps(sidx, separators=(",", ":"), ensure_ascii=False), "utf-8")
    # mtime=0 -> reproducible .gz bytes (same content = same file), like the
    # `gzip -n` pack-db.sh already uses: golden diffs become plain `diff -r`
    # and unchanged artifacts stop churning in deploys/mirrors.
    (out / "body-index.json.gz").write_bytes(gzip.compress(
        json.dumps(bidx, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8"), 9, mtime=0))
    return sidx
