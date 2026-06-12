#!/usr/bin/env python3
"""Page chrome and HTML primitives shared by the site writers.

CSS / SCRIPT are the site-wide stylesheet and client JS, published under
content-hashed asset names; page() wraps a body in the chrome. The small
helpers render the shared .sline list-row grammar, display names and the
SEO scaffolding. _BASE is the absolute site base URL: generate.build()
sets it (pagelib._BASE = ...) and everything here reads it at call time.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
from pathlib import Path

import threads
from names import clean as _clean_name

CSS = """
body{font:15px/1.5 system-ui,sans-serif;margin:0;color:#1a1a1a;background:#fafafa}
header{background:#338a3a;color:#fff;padding:14px 22px}
header a{color:#fff;text-decoration:none}
main{max-width:880px;margin:0 auto;padding:22px}
h1{font-size:20px} h2{font-size:16px;margin-top:26px;border-bottom:1px solid #ddd}
a{color:#338a3a} ul{padding-left:18px} li{margin:3px 0}
:root{accent-color:#338a3a}
input:focus,select:focus,textarea:focus{outline:2px solid #338a3a;outline-offset:1px}
.meta{color:#666;font-size:13px}
pre{white-space:pre-wrap;background:#fff;border:1px solid #e3e3e3;padding:14px;
    border-radius:6px;overflow:auto}
.mgrid{display:flex;flex-wrap:wrap;gap:4px 16px;margin:4px 0 8px}
.mgrid a{text-decoration:none} .mgrid a.mgactive{font-weight:600;text-decoration:underline}
.mgrid span{color:#767676}
.mpanel{margin:0 0 16px;padding:6px 0 0;border-top:1px solid #e3e3e3} .mpanel ul{margin:6px 0}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;
    color:#fff;background:#888;margin-right:6px;vertical-align:1px}
.badge.github{background:#24292f} .badge.list{background:#338a3a}
.badge.inbox{background:#2e7d32}
.msg{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:8px 20px 16px}
.tmsg{margin:14px 0}
.tmsg>summary{cursor:pointer;padding:4px 0;list-style:none}
.tmsg>summary::-webkit-details-marker{display:none}
/* one fold arrow everywhere: thread-page summaries and .sline toggles */
.tmsg>summary::before,.xpand::before{content:'\\25B8\\00A0';color:#338a3a;
    font-size:18px;vertical-align:-2px}
.tmsg[open]>summary::before,.xpand.thopen::before{content:'\\25BE\\00A0'}
/* same box for both body types (plain text .pt and HTML .md) */
.tmsg .pt,.tmsg .md{background:#fff;border:1px solid #e3e3e3;
    border-radius:6px;padding:12px 14px;margin-top:4px}
.plink{text-decoration:none;color:#338a3a;font-size:15px;vertical-align:-1px}
.plink:hover{color:#1b5e20}
.copytext{font:inherit;font-size:12px;color:#338a3a;background:#eef5ee;
    border:1px solid #cfe0cf;border-radius:4px;padding:0 6px;cursor:pointer}
.copytext:hover{background:#dcebdc}
.tmsg[open]>summary{border-bottom:1px solid #eee;margin-bottom:8px}
.msg h1{margin-top:10px}
.msg>.pt,.msg>.md{border:0;border-radius:0;padding:0}
.pt pre{white-space:pre-wrap;background:transparent;border:0;border-radius:0;
    padding:0;margin:0;overflow:auto}
.pt blockquote{margin:6px 0 6px 6px;padding-left:7px;
    border-left:2px solid #ccc;color:#555}
.pt,.md{background:#fff;border:1px solid #e3e3e3;padding:14px;border-radius:6px}
.md pre{background:#f6f8fa} .md img{max-width:100%}
/* one rhythm everywhere: blocks have no margin, so spacing comes only from
   line-height and the single <br> blank lines kept by the sanitizer */
.md p,.md div,.md table{margin:0}
.md blockquote{margin:6px 0 6px 6px;padding-left:7px;
    border-left:2px solid #ddd;color:#555}
ul.thread,ul.thread ul{list-style:none;padding-left:15px;
    border-left:1px solid #e8e8e8;margin:2px 0}
ul.thread li{margin:4px 0}
.att{margin:14px 0;padding:10px 14px;background:#fff7e6;
    border:1px solid #f0d9a8;border-radius:6px}
.att ul{margin:6px 0 0} .att li{margin:3px 0}
.att img{display:block;max-width:100%;max-height:260px;background:#fff;
    border:1px solid #e3e3e3;border-radius:4px;margin:2px 0 1px}
.hsearch{float:right;font-size:13px;opacity:.85}
.clip{font-size:12px;opacity:.75;cursor:default}
pre a{color:#338a3a}
#q{width:100%;padding:9px 11px;font-size:15px;box-sizing:border-box;
    border:1px solid #ccc;border-radius:6px}
#res{list-style:none;padding:0} #res>li{margin:0;padding:7px 2px;border-bottom:1px solid #eee}
.sline{display:flex;align-items:baseline;gap:6px;font-size:14px}
.tsub{font-weight:600;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sline .meta{color:#666;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plinks{margin-left:auto;flex:none;display:flex;gap:10px;white-space:nowrap}
.plink{font-size:14px;text-decoration:none;opacity:.65}
.plink:hover{opacity:1}
.sprev{margin:3px 0 0 1.3em}
.pline{color:#555;font-size:13px;margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pline mark,.thmsgs mark{background:#fff3b0;color:#1a1a1a;border-radius:2px;padding:0 1px}
.hero{text-align:center;padding:42px 22px 34px;margin:-22px -22px 22px;
    background:radial-gradient(120% 140% at 50% 0,#4caf50 0,#338a3a 45%,#1b5e20 100%);
    color:#fff;box-shadow:inset 0 -1px 0 rgba(255,255,255,.08)}
.hero h1{font-size:34px;margin:0;font-weight:700;letter-spacing:.2px;
    text-shadow:0 1px 2px rgba(0,0,0,.25)}
.hero .tag{font-size:16px;opacity:.92;margin:10px 0 0;font-weight:300}
.hero .meta{color:#c8e6c9;font-size:12.5px;margin:14px 0 0;letter-spacing:.3px}
.hero .meta b{color:#fff;font-weight:600}
.altlinks{text-align:center;font-size:13px;color:#666;margin:6px 0 18px}
.bars{margin:0}
.bar{display:grid;grid-template-columns:46px 1fr 60px;align-items:center;
    gap:9px;font-size:13px;margin:3px 0}
.bar .byr{color:#338a3a;text-decoration:none} .bar .bc{color:#666;text-align:right}
.ubar{display:grid;grid-template-columns:175px 1fr 56px;align-items:center;
    gap:9px;font-size:13px;margin:3px 0}
.ubar .un{color:#338a3a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ubar .bc{color:#666;text-align:right}
.stath{font-size:15px;font-weight:600;margin:22px 0 8px}
.bars .stath:first-child{margin-top:0}
.btrack{background:#e9f1e9;border-radius:3px}
.bbar{display:block;height:11px;background:#338a3a;border-radius:3px;min-width:2px}
.recent{margin:0;padding-left:2px;list-style:none}
.recent li{margin:6px 0}
.tbtn{font:inherit;color:#338a3a;background:#e9f1e9;border:1px solid #cfe0cf;
    border-radius:5px;padding:6px 14px;cursor:pointer}
.tbtn:hover{background:#dcebdc}
.xpand{cursor:pointer}    /* expand-in-place toggle (search/threads/archive) */
.mlist{list-style:none;padding-left:2px}      /* archive flat lists: no bullets */
.mlist li{margin:4px 0}
.thmsgs{margin:4px 0 10px 6px;padding-left:16px;border-left:2px solid #d9e6d9;
    list-style:none}    /* expanded thread: indent + rail to show nesting */
.thmsgs li{margin:2px 0}
.thmsgs .msg h1{font-size:15px;margin:0 0 6px}   /* thread name atop an expanded message */
/* quoted text in a message body: folded by default (the parent is shown above
   in the thread, so quotes are duplication); click to expand. */
details.q{margin:6px 0}
details.q>summary{cursor:pointer;color:#338a3a;font-size:13px;list-style:none}
details.q>summary::-webkit-details-marker{display:none}
/* arrow only (no "quoted text" label); real char -> never the browser default */
details.q>summary .ar{display:inline-block;font-size:18px;line-height:1;
    vertical-align:-2px;transition:transform .12s}
details.q[open]>summary .ar{transform:rotate(90deg)}
details.q>blockquote,details.q>.md-q{margin-top:4px}
"""

# Linked (not inlined) -> behaviour tweaks need no page re-render. Folds text
# proven to duplicate an earlier message; runs on load and is called again
# after the Threads tab injects a thread inline.
SCRIPT = """
function foldQuotes(root){
  root=root||document;
  var KG=6;   // word-shingle size for content-dedup (declared early: used below)
  // summary = just an arrow (real char span, so a stale stylesheet never shows the
  // browser default "Details"); the .ar span rotates on open via CSS. No text label.
  function qsum(){ var s=document.createElement('summary');
    var a=document.createElement('span'); a.className='ar';
    a.textContent=String.fromCharCode(0x25B8); s.appendChild(a); return s; }
  // Visible words outside existing quote folds. Every folding path uses the
  // same word-based never-hollow rule; character counts let "Yes" pass.
  function visWords(el){ var t=0; (function w(x){ [].forEach.call(x.childNodes,function(n){
    if(n.nodeType===1){ if(n.classList&&n.classList.contains('q')) return; w(n); }
    else if(n.nodeType===3) t+=(n.textContent.match(/\\S+/g)||[]).length; }); })(el);
    return t; }
  // "----- Original Message -----" separator (specific multi-locale phrases, not a
  // generic dashed line -> a STRONG forward signal without false matches).
  var ORIG=/-{2,}\\s*(Original Message|Ursprüngliche Nachricht|Message d'origine|Mensaje original|Messaggio originale|Oorspronkelijk bericht|Oprindelig meddelelse|Ursprungligt meddelande|Opprinnelig melding)\\s*-{2,}/i;
  // confidentiality footer openers (multi-locale) -- kept OUT of the quote fold.
  var DISC=/(^|[\\n>])[ \\t]*(This (e-?mail|message|email|communication)\\b[^\\n]{0,60}\\b(intended|confidential|privileged|may contain|and any|contains|is meant)|CONFIDENTIAL(ITY)?\\b|The information (contained|in this|transmitted)|Diese E-?Mail\\b[^\\n]{0,50}(vertraulich|Empfänger)|Ce (courriel|message)\\b[^\\n]{0,50}(confidentiel|destin)|Este (mensaje|correo)\\b|LEGAL (NOTICE|DISCLAIMER)|NOTICE:)/i;
  // a single header-field line (From:/Sent:/To:/Subject:...) or an attribution line
  // ("On ... wrote:"); used to pull a quote's lead-in into a content-dedup fold.
  var HDR=/^[ \\t>]*(From|Von|De|Da|Van|Fra|Från|Sent|Gesendet|Date|Sendt|Skickat|To|An|Til|Cc|Bcc|Subject|Betreff|Objet|Emne|Ämne|Reply-To|Envoy\\w*|Enviad\\w*)\\b[^\\n]*:/i;
  var FROMHDR=/^[ \\t>]*(From|Von|De|Da|Van|Fra|Från)\\b[^\\n]*:/i;
  var ATTR=/\\b(wrote|schrieb|escribi[oó]|escreveu|skrev|ha scritto)\\b|a [eé]crit|^[ \\t>]*(On|Le|El|Am|Op|Den|P[aå]|Il giorno)\\b/i;
  contentDedup(root);   // content-based: fold blocks duplicated from earlier messages
  mergeAdjacent(root);   // collapse consecutive folds (any origin) into one toggle
  [].forEach.call(root.querySelectorAll('details.q details.q'), unwrap);  // flatten nested folds -> one toggle
  [].forEach.call(root.querySelectorAll('details.q'), maybeOpen);  // short quotes: open by default
  // reconstruct logical lines from a fold's DOM (text nodes + <br>/block breaks)
  // -- textContent alone carries no newlines in HTML or flowed-text bodies.
  function domLines(d){
    var s='';
    (function w(el){ [].forEach.call(el.childNodes,function(n){
      if(n.nodeType===3) s+=n.textContent;
      else if(n.nodeType===1){
        if(n.tagName==='SUMMARY') return;
        if(n.tagName==='BR') s+='\\n';
        else if(/^(P|DIV|LI|BLOCKQUOTE|TR|PRE|H[1-6]|UL|OL|TABLE)$/.test(n.tagName)){ s+='\\n'; w(n); s+='\\n'; }
        else w(n);
      }
    }); })(d);
    return s.split('\\n');
  }
  // Show a fold expanded by default when it's a SHORT, real quoted body -- not a
  // header/attribution/disclaimer. Measured in BOTH lines and chars so a single
  // long "flowed" line counts as long. No footer/separator/header line allowed.
  function maybeOpen(d){
    if(!d.querySelector(':scope>summary')) d.insertBefore(qsum(),d.firstChild);  // never show browser default "Details"
    if(d.classList.contains('sig')){ d.open=false; return; }  // repeated signature: short but never auto-open
    var content=domLines(d).filter(function(l){ return l.replace(/[\\s>\\ufeff]/g,'').length>0; });
    if(content.some(function(l){ return DISC.test(l)||ORIG.test(l)||HDR.test(l); })){ d.open=false; return; }
    var bodyChars=content.filter(function(l){ return !ATTR.test(l); }).join('').replace(/[\\s>]/g,'').length;
    var totalChars=content.join(' ').replace(/\\s+/g,' ').trim().length;
    d.open = content.length<=6 && totalChars<=400 && bodyChars>=12;
  }
  // Merge folds that are consecutive in DOCUMENT ORDER (nothing but whitespace
  // between them, even across different containers) into one toggle -- so an
  // attribution fold + its quoted body, or adjacent quote regions, read as one.
  function mergeAdjacent(root){
    function headerBlock(s){ var n=0, from=false;
      s.split('\\n').forEach(function(line){ if(HDR.test(line)){ n++; if(FROMHDR.test(line)) from=true; } });
      return from&&n>=2; }
    var dets=[].slice.call(root.querySelectorAll('details.q'));
    for(var i=1;i<dets.length;i++){
      var a=dets[i-1], b=dets[i];
      if(!a.parentNode||!b.parentNode||a.contains(b)||b.contains(a)){ continue; }
      // a split-off signature fold keeps its own "signature" label -- merging
      // it into the neighbouring quote fold would undo the server's split.
      if(a.classList.contains('sig')||b.classList.contains('sig')) continue;
      // NEVER merge across message bodies: a Range spanning two .tmsg blocks
      // splits the second <details> into a summary-less shell (browsers then
      // show their locale's default label -- "Détails", "Details", ...) and
      // swallows the next message into the previous fold. Triggered by a
      // bottom quote followed by a short reply + attribution next message.
      var ca=a.closest('.pt,.md'), cb=b.closest('.pt,.md');
      if(!ca||ca!==cb) continue;
      var rg=document.createRange(), gap;
      try{ rg.setStartAfter(a); rg.setEndBefore(b); gap=rg.toString(); }catch(e){ continue; }
      // merge if nothing real sits between -- whitespace, OR just an attribution /
      // header / separator line (it belongs to the next quote). A real reply -> keep.
      // The attribution test needs the SHAPE, not the vocabulary: a real sentence
      // like "you wrote a slightly differing set of CVE-IDs:" contains "wrote",
      // sits under 150 chars, and ATTR.test() swallowed it into the fold. Only a
      // gap that STARTS with an attribution opener or ENDS with the "wrote:" verb
      // form is a quote lead-in.
      var ATTRGAP=/^[>\\s]*(On|Le|El|Am|Op|Den|P[aå]|Il giorno)\\b[\\s\\S]*:$|(wrote|schrieb|escribi[oó]|escreveu|skrev|ha scritto|a [eé]crit)\\s*:$/i;
      var g=gap.replace(/[\\s\\ufeff\\u200b]/g,''), gt=gap.replace(/\\s+/g,' ').trim();
      if(g!=='' && !(gt.length<150 &&
                     (ATTRGAP.test(gt)||headerBlock(gap)||ORIG.test(gap)))) continue;
      a.appendChild(rg.extractContents());       // pull the gap (attribution/ws) into a
      while(b.firstChild){                        // then b's content (minus summary)
        if(b.firstChild.tagName==='SUMMARY'){ b.removeChild(b.firstChild); continue; }
        a.appendChild(b.firstChild);
      }
      var p=b.parentNode; p.removeChild(b);      // drop b, then prune empty wrappers
      while(p && p!==root && !(p.classList&&(p.classList.contains('md')||p.classList.contains('pt')))
            && p.children.length===0 && (p.textContent||'').replace(/[\\s\\ufeff]/g,'')===''){
        var pp=p.parentNode; if(pp) pp.removeChild(p); p=pp;
      }
      dets[i]=a;                                 // a carries on as the merged fold
    }
  }
  // {n:textNode,o:offset} at a text offset within body (TreeWalker over text).
  function nodeAt(body, off){
    var w=document.createTreeWalker(body,NodeFilter.SHOW_TEXT), pos=0, node;
    while((node=w.nextNode())){ var L=node.textContent.length;
      if(pos+L>=off){ return {n:node, o:off-pos}; } pos+=L; }
    return null;
  }
  // fold body[startOff .. endOff] into a <details.q> via a Range (splits text
  // nodes as needed). endOff>=length means "to the end".
  function foldRange(body, startOff, endOff){
    var s=nodeAt(body,startOff); if(!s||!body.lastChild) return;
    if(s.n.parentElement && s.n.parentElement.closest('details.q')) return; // start already folded
    try{ var rg=document.createRange(); rg.setStart(s.n,s.o);
      var e=(endOff<(body.textContent||'').length)?nodeAt(body,endOff):null;
      // if the END falls inside an existing fold, CLIP to just before it rather
      // than splitting it (or skipping the whole region) -- fold the part up to it.
      var ef=(e && e.n.parentElement)?e.n.parentElement.closest('details.q'):null;
      if(ef) rg.setEndBefore(ef);
      else if(e) rg.setEnd(e.n,e.o);
      else rg.setEndAfter(body.lastChild);
      if(rg.collapsed) return;
      var d=document.createElement('details'); d.className='q';
      d.appendChild(qsum());
      d.appendChild(rg.extractContents()); rg.insertNode(d);
      if(visWords(body)<3) unwrap(d);     // never fold the WHOLE message (bottom-post)
    }catch(err){}
  }
  function unwrap(d){   // undo a fold: move its content (minus summary) back, drop it
    var p=d.parentNode; if(!p) return;
    while(d.firstChild){ if(d.firstChild.tagName==='SUMMARY'){ d.removeChild(d.firstChild); continue; }
      p.insertBefore(d.firstChild,d); }
    p.removeChild(d);
  }
  // Content-dedup (markup-agnostic): walk the thread's messages in order and fold
  // every block whose WORD k-grams duplicate text seen in EARLIER messages.
  // Word-shingles (not lines) -> robust to re-wrapping, punctuation and language.
  // Folds inline quotes too (#1); runs back-to-front so offsets stay valid.
  function normWord(raw){
    try{return raw.normalize('NFKC').toLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,'');}
    catch(e){return raw.toLowerCase().replace(/[^a-z0-9]+/g,'');}
  }
  function toks(text){   // normalized words with their raw text offsets
    var re=/\\S+/g, m, o=[];
    while((m=re.exec(text))){ var raw=m[0], w=normWord(raw);
      if(!w) continue;
      // Trim leading/trailing punctuation from the OFFSETS, not just from the
      // normalized word: adjacent block elements yield no separating space in
      // textContent, so an attribution "ecrit :" can glue to the quote start as a
      // single token ":Just". Folding from the raw start would split the ":" into
      // the quote fold (it ends up on its own line under the toggle). Fold the WORD.
      var lead=raw.search(/[\\p{L}\\p{N}]/u); if(lead<0) lead=raw.search(/[a-z0-9]/i);
      if(lead<0) lead=0;
      var trail=raw.length-1;
      while(trail>lead && normWord(raw[trail])==='') trail--;
      o.push({w:w, off:m.index+lead, end:m.index+trail+1}); }
    return o;
  }
  function shingles(t){ var s=[]; for(var i=0;i+KG<=t.length;i++){
    var g=''; for(var j=0;j<KG;j++) g+=t[i+j].w+' '; s.push(g); } return s; }
  // A logical line's [start,end) char offsets in body.textContent, split on <br>
  // and block boundaries. Counts ALL text (including already-folded content) so the
  // offsets stay aligned with nodeAt()/foldRange(); the line text is kept for the
  // attribution check. This is the unit contentDedup folds -- a whole line at a
  // time -- so a text node is never split (no dangling ">-"/"word," fragments).
  function lineSpans(body){
    var spans=[], start=0, pos=0, text='';
    function brk(skip){ spans.push({start:start, end:pos, text:text});
      pos+=skip||0; start=pos; text=''; }
    function add(s,inPre){ if(!inPre){ text+=s; pos+=s.length; return; }
      var p=s.split('\\n'); for(var i=0;i<p.length;i++){ text+=p[i]; pos+=p[i].length;
        if(i<p.length-1) brk(1); } }
    (function w(el,inPre){ [].forEach.call(el.childNodes,function(n){
      if(n.nodeType===3){ add(n.textContent,inPre); }
      else if(n.nodeType===1){
        if(n.tagName==='BR'){ brk(0); }
        else if(/^(P|DIV|LI|BLOCKQUOTE|TR|PRE|H[1-6]|UL|OL|TABLE|DETAILS)$/.test(n.tagName)){
          brk(0); w(n,inPre||n.tagName==='PRE'); brk(0); }
        else w(n,inPre);
      }
    }); })(body,false);
    brk(0);
    return spans;
  }
  // Content-dedup, LINE-granular and conservative: a logical line is foldable
  // only when EVERY normalized word duplicates EARLIER messages. Fold maximal
  // exact runs that form a real BLOCK -- >=2 lines, a trailing (bottom) quote,
  // or a run led by an attribution ("X wrote:"). Any changed or uncovered word
  // keeps its whole line visible. Folds whole lines only, so a text node is
  // never split.
  function contentDedup(root){
    var bodies=root.querySelectorAll('.tmsg .pt,.tmsg .md');
    if(!bodies.length) bodies=root.querySelectorAll('.pt,.md');
    var prior={};
    [].forEach.call(bodies, function(body){
      var full=body.textContent||'', t=toks(full);
      if(t.length>=KG){
        var cov=new Array(t.length), i, j, g;
        for(i=0;i+KG<=t.length;i++){ g=''; for(j=0;j<KG;j++) g+=t[i+j].w+' ';
          if(prior[g]) for(j=0;j<KG;j++) cov[i+j]=true; }
        var spans=lineSpans(body), nL=spans.length, nw=[], nc=[];
        for(i=0;i<nL;i++){ nw[i]=0; nc[i]=0; }
        var li=0;                                   // map each word to its line
        for(i=0;i<t.length;i++){ while(li<nL-1 && t[i].off>=spans[li].end) li++;
          nw[li]++; if(cov[i]) nc[li]++; }
        var quoted=[];                              // every word is duplicated
        for(i=0;i<nL;i++) quoted[i]=nw[i]>0 && nc[i]===nw[i];
        var runs=[]; i=0;                           // maximal runs of quoted lines
        while(i<nL){
          if(!quoted[i]){ i++; continue; }
          var s=i, e=i; j=i+1;
          while(j<nL){ if(quoted[j]){ e=j; j++; }
            else if(nw[j]===0){ j++; }              // blank line -> bridge the run
            else break; }
          runs.push([s,e]); i=j;
        }
        for(var r=runs.length-1;r>=0;r--){
          var a=runs[r][0], b=runs[r][1], tot=0, k;
          for(k=a;k<=b;k++) tot+=nc[k];
          if(tot<12) continue;                      // too little duplication to fold
          var multi=b>a, trailing=true;             // a bottom quote reaches the end
          for(k=b+1;k<nL;k++){ if(nw[k]>0 && !quoted[k]){ trailing=false; break; } }
          var attr=false;                           // run led by an "X wrote:" line
          for(k=a-1;k>=0;k--){ if(nw[k]===0) continue;
            attr=ATTR.test(spans[k].text) && /:\\s*$/.test(spans[k].text); break; }
          if(multi||trailing||attr)
            foldRange(body, spans[a].start, spans[b].end);  // fold whole lines
        }
      }
      shingles(t).forEach(function(g){ prior[g]=1; });
    });
  }
}
function siteRoot(){
  return new URL((document.body&&document.body.dataset.root)||'./',location.href).href;
}
// The message text (header + body), used by the copy-content button.
function msgText(m){
  var s=m.querySelector('summary'), head='';
  if(s){var c=s.cloneNode(true);
    c.querySelectorAll('.copytext,.plink').forEach(function(e){e.remove();});
    head=c.textContent.replace(/\\s+/g,' ').trim();}
  var b=m.querySelector('.pt,.md');
  return (head?head+'\\n\\n':'')+(b?b.innerText.trim():'');
}
// Turn each .copy marker into a NATIVE permalink (<a>, no fold) + a button that
// copies the message's (or, for a thread marker, the whole thread's) content.
function enhance(root){
  (root||document).querySelectorAll('.copy').forEach(function(b){
    var href=b.dataset.href||'', isThread=href.indexOf('#m-')<0;
    var a=document.createElement('a'); a.className='plink'; a.title='permalink';
    a.setAttribute('aria-label','permalink');
    a.href=new URL(href,siteRoot()).href; a.innerHTML='&#128279;';  // link icon
    var c=document.createElement('button'); c.className='copytext'; c.type='button';
    c.innerHTML='&#128203;'; c.title=isThread?'copy whole thread':'copy this message';
    c.setAttribute('aria-label',c.title);
    if(isThread) c.setAttribute('data-thread','');
    var f=document.createDocumentFragment();
    f.appendChild(a); f.appendChild(document.createTextNode(' ')); f.appendChild(c);
    b.replaceWith(f);
  });
}
document.addEventListener('click',function(ev){
  var b=ev.target.closest&&ev.target.closest('.copytext'); if(!b) return;
  ev.preventDefault();
  var text;
  if(b.hasAttribute('data-thread')){
    var r=b.closest('.thread')||document;
    text=[].map.call(r.querySelectorAll('.tmsg'),msgText).join('\\n\\n----------\\n\\n');
  } else { var m=b.closest('.tmsg'); text=m?msgText(m):''; }
  var old=b.innerHTML;
  function ok(){b.innerHTML='\\u2713';setTimeout(function(){b.innerHTML=old;},1200);}
  if(navigator.clipboard) navigator.clipboard.writeText(text).then(ok,function(){});
});
// THE inline-expansion pipeline: fetch a generated page, extract `o.sel`,
// strip its chrome (`o.strip` + optional `o.post`), rewrite ../ links (callers
// sit at the site root), inject into `box`, then enhance/fold/tidy and
// highlight `o.terms`. Every expander (Threads tab, search/archive message
// expand, the Archive month accordion) goes through here, so failure UX,
// link rewriting and highlighting cannot drift between them.
// Racing loads on one box: the latest call wins (per-box token).
function inject(box,url,o){
  o=o||{};
  var tok=(box._itok=(box._itok||0)+1);
  box.setAttribute('aria-busy','true');
  box.innerHTML='<p class=meta>Loading\\u2026</p>';
  function fail(){
    if(box._itok!==tok) return;
    box.removeAttribute('aria-busy');
    box.innerHTML=o.fallback
      ?"<p class=meta><a href='"+o.fallback+"'>open \\u2192</a></p>"
      :'<p class=meta>Failed to load.</p>';
  }
  fetch(url).then(function(r){return r.text();}).then(function(t){
    if(box._itok!==tok) return;
    if(o.sel){
      var el=new DOMParser().parseFromString(t,'text/html').querySelector(o.sel);
      if(!el){fail();return;}
      if(o.strip) el.querySelectorAll(o.strip).forEach(function(n){n.remove();});
      if(o.post) o.post(el);
      el.querySelectorAll('[href^="../"]').forEach(function(a){a.setAttribute('href',a.getAttribute('href').slice(3));});
      el.querySelectorAll('[src^="../"]').forEach(function(a){a.setAttribute('src',a.getAttribute('src').slice(3));});
      box.innerHTML=el.innerHTML;
    } else {
      box.innerHTML=t;            // pre-rendered fragment (frag/<month>.html)
    }
    box.removeAttribute('aria-busy');
    if(window.enhance)enhance(box); if(window.foldQuotes)foldQuotes(box);
    if(window.tidyBreaks)tidyBreaks(box);
    if(o.terms&&o.terms.length)hlTerms(box,o.terms);
  }).catch(fail);
}
// terms currently in the search box, for highlighting injected content
function curTerms(){
  var qel=document.getElementById('q');
  return qel?qel.value.toLowerCase().split(/\\s+/).filter(Boolean):[];
}
// fetch a whole thread and inject it inline (Threads/Search/Archive expand in
// place); search terms are highlighted in the expanded thread too.
function loadThread(box,tid,fallback){
  inject(box,'thread/'+tid+'.html',{sel:'.thread',strip:'h1,.thread>.meta',
    fallback:fallback,terms:curTerms(),
    post:function(el){              // drop the "<- index" footer paragraphs
      el.querySelectorAll('a[href$="index.html"]').forEach(function(a){
        var p=a.closest('p'); if(p&&p.parentNode===el)p.remove();});}});
}
// highlight search terms (wrap in <mark>) inside an element's text nodes.
function hlTerms(box,terms){
  if(!terms||!terms.length) return;
  var w=document.createTreeWalker(box,NodeFilter.SHOW_TEXT), nodes=[],n;
  while((n=w.nextNode())) nodes.push(n);
  nodes.forEach(function(tn){
    var t=tn.textContent, low=t.toLowerCase(), frag=null, pos=0;
    while(pos<t.length){
      var at=-1,len=0;
      for(var k=0;k<terms.length;k++){ var p=low.indexOf(terms[k],pos);
        if(p>=0&&(at<0||p<at)){at=p;len=terms[k].length;} }
      if(at<0) break;
      if(!frag) frag=document.createDocumentFragment();
      if(at>pos) frag.appendChild(document.createTextNode(t.slice(pos,at)));
      var mk=document.createElement('mark'); mk.textContent=t.slice(at,at+len);
      frag.appendChild(mk); pos=at+len;
    }
    if(frag){ if(pos<t.length) frag.appendChild(document.createTextNode(t.slice(pos)));
      tn.parentNode.replaceChild(frag,tn); }
  });
}
// fetch one canonical message page and inject its body inline; highlight terms.
// Body only -- the subject/author/date already show on the result/archive line.
function loadMsg(box,mid,terms){
  inject(box,'msg/'+mid+'.html',{sel:'.msg',strip:'h1,p.meta',
    fallback:'msg/'+mid+'.html',terms:terms});
}
// ONE list row for every surface -- search results and the Threads tab call
// this; the month pages emit the same markup server-side (generate._msg_line;
// a ui_test asserts the contract). Grammar:
//   [subject toggle] [· author · when [· N messages]] [clip] [thread,permalink]
function lineEl(o){
  var li=document.createElement('li');
  var sl=document.createElement('div'); sl.className='sline';
  var a=document.createElement('a'); a.className='tsub xpand';
  if(o.mid) a.setAttribute('data-mid',o.mid);
  else if(o.tid) a.setAttribute('data-tid',o.tid);
  a.href=o.href; a.textContent=o.subject||'(no subject)';
  sl.appendChild(a);
  var au=document.createElement('span'); au.className='meta';
  au.textContent=' \\u00b7 '+(o.author||'')+' \\u00b7 '+(o.when||'')+
    (o.count?' \\u00b7 '+o.count+' message'+(o.count>1?'s':''):'');
  sl.appendChild(au);
  if(o.att){var c=document.createElement('span'); c.className='clip';
    c.title=o.att+' attachment'+(o.att>1?'s':''); c.textContent='\\u{1F4CE}';
    c.setAttribute('role','img'); c.setAttribute('aria-label',c.title);
    sl.appendChild(c);}
  var pl=document.createElement('span'); pl.className='plinks';
  if(o.threadHref){var lt=document.createElement('a'); lt.className='plink';
    lt.href=o.threadHref; lt.textContent='\\u{1F9F5}';
    lt.title='Open the thread'; lt.setAttribute('aria-label',lt.title);
    pl.appendChild(lt);}
  if(o.msgHref){var lr=document.createElement('a'); lr.className='plink';
    lr.href=o.msgHref; lr.textContent='\\u{1F517}';
    lr.title='Permalink to the raw message';
    lr.setAttribute('aria-label',lr.title); pl.appendChild(lr);}
  sl.appendChild(pl);
  li.appendChild(sl);
  return li;
}
// a link marked .xpand expands its content inline: data-mid -> that single
// MESSAGE (search/archive), data-tid -> the whole THREAD (Threads tab).
document.addEventListener('click',function(e){
  var a=e.target.closest&&e.target.closest('.xpand[data-mid],.xpand[data-tid]');
  if(!a) return;
  e.preventDefault();
  var host=a.closest('li')||a.parentNode;
  var prev=host.querySelector(':scope > .sprev');   // search preview (hide while full)
  var box=host.querySelector(':scope > .thmsgs');
  if(box){ box.hidden=!box.hidden; var open=!box.hidden;
    a.classList.toggle('thopen',open); if(prev) prev.hidden=open; return; }
  box=document.createElement('div'); box.className='thmsgs'; host.appendChild(box);
  a.classList.add('thopen'); if(prev) prev.hidden=true;
  if(a.hasAttribute('data-tid'))
    loadThread(box,a.getAttribute('data-tid'),a.href);
  else
    loadMsg(box,a.getAttribute('data-mid'),curTerms());
});
// collapse runs of >2 consecutive <br> (typed blank lines) down to 2 -- keeps
// paragraph spacing (<br><br>) but trims a message padded with many empty lines.
function tidyBreaks(root){
  [].forEach.call((root||document).querySelectorAll('.md,.pt'),function(body){
    [].forEach.call(body.querySelectorAll('br'),function(br){
      var n=0,p=br.previousSibling;
      while(p && ((p.nodeType===3 && !p.textContent.trim())||(p.nodeType===1 && p.tagName==='BR'))){
        if(p.nodeType===1)n++; p=p.previousSibling; }
      if(n>=2) br.parentNode.removeChild(br);
    });
  });
}
document.addEventListener('DOMContentLoaded',function(){enhance(document);foldQuotes(document);tidyBreaks(document);});
"""


# Content-hashed asset names: the URL changes only when the file changes, so the
# browser/CDN always pick up a new version (no stale cache, no hard-refresh)
# while unchanged assets stay cached forever.
_JS_NAME = f"script.{hashlib.sha1(SCRIPT.encode()).hexdigest()[:10]}.js"
_CSS_NAME = f"style.{hashlib.sha1(CSS.encode()).hexdigest()[:10]}.css"

# Absolute site base URL (no trailing slash), e.g.
# https://xymon-monitoring.github.io/xymon-discussion-public. Set by build()
# from --base-url / $BASE_URL (auto-derived on GitHub Actions). When empty the
# build stays fully portable: canonical tags and the sitemap are simply skipped.
_BASE = ""

# Tiny standalone icon (a green X) -> no /favicon.ico 404 on every page view.
_FAVICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
            "<rect width='16' height='16' rx='3' fill='#338a3a'/>"
            "<path d='M4.5 4.5l7 7M11.5 4.5l-7 7' stroke='#fff' "
            "stroke-width='2.2' stroke-linecap='round'/></svg>\n")


def page(title: str, body: str, root: str = "", header: bool = True,
         scripts: bool = True, desc: str | None = None,
         canon: str | None = None) -> str:
    # root is "" for top-level pages and "../" for pages under msg/, so the
    # header links resolve from any depth. header=False omits the top bar (the
    # home page carries the title+search in its hero, so the bar is redundant).
    # scripts=False -> a canonical, feature-free page (no fold/copy JS).
    # desc -> <meta name=description>; canon (site-relative path, '' = root)
    # -> <link rel=canonical>, emitted only when an absolute base is known.
    bar = (f"<header><a href={root}index.html>Xymon Mailing List Archive</a> "
           f"<a class=hsearch href={root}index.html>search</a></header>"
           if header else "")
    js = (f"<script src='{root}{_JS_NAME}' defer></script>" if scripts else "")
    meta = (f'<meta name=description content="{html.escape(desc)}">'
            if desc else "")
    if _BASE and canon is not None:
        meta += f"<link rel=canonical href='{_BASE}/{canon}'>"
    return (
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>{meta}"
        f"<link rel=icon type='image/svg+xml' href='{root}favicon.svg'>"
        f"<link rel=stylesheet href='{root}{_CSS_NAME}'>{js}</head>"
        f"<body data-root='{root}'>{bar}<main>{body}</main></body></html>"
    )


def e(s: str | None) -> str:
    return html.escape(s or "")


def short_date(r) -> str:
    """Compact list date: 2024-07-05 12:32 (full original kept on msg page)."""
    iso = r["date_iso"]
    if iso:
        return iso[:16].replace("T", " ")
    return r["date_raw"] or ""


def _clip(n: int) -> str:
    if not n:
        return ""
    lbl = f"{n} attachment{'s' if n > 1 else ''}"
    return (f" <span class=clip role=img title='{lbl}' "
            f"aria-label='{lbl}'>&#128206;</span>")


def msg_name(r) -> str:
    """Stable message-page stem: a hash of the (globally unique) Message-Id.

    The SQLite rowid churns whenever a month is re-crawled (DELETE+INSERT), so
    using it would break permalinks every CI run. The msgid hash is permanent.
    """
    mid = r["msgid"]
    if mid:
        return threads.stable_id(mid, 16)
    return f"x{r['id']}"


def _xp(r) -> str:
    """Make the whole subject link an expand-in-place MESSAGE toggle (arrow +
    name) in the shared .sline grammar (same classes as script.js lineEl);
    the global handler injects msg/<id>.html inline. href is the no-JS
    fallback."""
    return (f"class='tsub xpand' data-mid='{msg_name(r)}' "
            f"href='msg/{msg_name(r)}.html'")


def _msg_line(r, att_counts, htid=None) -> str:
    """One message list row in the site-wide .sline grammar -- the SAME
    structure script.js's lineEl() builds for search results and the Threads
    tab (a ui_test asserts the contract): subject expand-toggle, then
    "· author · date", attachment clip, and right-aligned thread/permalink
    icons. Month pages show date+time (within one month, time IS the order);
    the JS surfaces slice to the date."""
    mid = msg_name(r)
    # With a thread_id column (production) honour it exactly -- a null stays a
    # plain msg link, matching the thread pass. Only a DB WITHOUT the column
    # (standalone) falls back to the computed stable thread id (htid).
    if "thread_id" in r.keys():
        tid = r["thread_id"]
    else:
        tid = htid.get(r["id"]) if htid else None
    thref = f"thread/{tid}.html#m-{mid}" if tid else f"msg/{mid}.html"
    return (f"<li><div class=sline><a {_xp(r)}>"
            f"{e(r['subject']) or '(no subject)'}</a>"
            f"<span class=meta> &middot; {e(whom(r))} &middot; "
            f"{e(short_date(r))}</span>"
            f"{_clip(att_counts.get(r['msgid'], 0))}"
            f"<span class=plinks>"
            f"<a class=plink href='{thref}' title='Open the thread' "
            f"aria-label='Open the thread'>&#129525;</a>"
            f"<a class=plink href='msg/{mid}.html' title='Permalink to the "
            f"raw message' aria-label='Permalink to the raw message'>"
            f"&#128279;</a></span></div>")


def _bar_row(cls: str, label: str, count: int, peak: int) -> str:
    """One horizontal stat bar (label + track + count): .bar / .ubar rows."""
    return (f"<div class={cls}>{label}"
            f"<span class=btrack><span class=bbar "
            f"style='width:{round(100 * count / peak)}%'></span></span>"
            f"<span class=bc>{count:,}</span></div>")


def _human(n: int) -> str:
    return f"{n} B" if n < 1024 else (
        f"{n/1024:.1f} KB" if n < 1024 * 1024 else f"{n/1048576:.1f} MB")


def _safe(name: str) -> str:
    return (name or "file").replace("/", "_").replace("\\", "_").lstrip(".") \
        or "file"


_NAMES: dict = {}       # pseudonymised from_email -> best-known display name


def whom(r) -> str:
    """Display name: the row's own name, else a name seen for the same sender
    elsewhere (so messages sent with a bare address still show the person's
    name), else the (pseudonymised) address. 'Last, First' is normalised."""
    fn = r["from_name"]
    name = fn if (fn and not fn.endswith("@xymon.invalid")) \
        else _NAMES.get(r["from_email"])
    if name:
        return _clean_name(name)
    return r["from_email"] or "(unknown)"


def _meta_desc(r) -> str:
    """Short plain-text summary for a message page's <meta name=description>."""
    b = re.sub(r"\s+", " ", r["body"] or "").strip()
    head = f"{whom(r)} · Xymon mailing list"
    s = f"{head}: {b}" if b else head
    return s[:157] + "…" if len(s) > 158 else s


def _github_base() -> str:
    """Default absolute base URL when building on GitHub Actions (Pages)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    owner = owner.lower()
    if name.lower() == f"{owner}.github.io":     # user/org page: served at /
        return f"https://{owner}.github.io"
    return f"https://{owner}.github.io/{name}"


def _write_sitemaps(out: Path, paths: list[str], chunk: int = 45000) -> None:
    """sitemap.xml over `paths` (site-relative, '' = root). Above `chunk` URLs
    (protocol limit 50k/file) the parts go to sitemap-N.xml behind an index."""
    def urlset(ps):
        return ("<?xml version='1.0' encoding='UTF-8'?>\n"
                "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                + "".join(f"<url><loc>{html.escape(f'{_BASE}/{p}')}</loc></url>"
                          for p in ps)
                + "</urlset>\n")
    for old in out.glob("sitemap-*.xml"):        # stale parts from a prior run
        old.unlink()
    if len(paths) <= chunk:
        (out / "sitemap.xml").write_text(urlset(paths), "utf-8")
        return
    parts = [paths[i:i + chunk] for i in range(0, len(paths), chunk)]
    for n, ps in enumerate(parts, 1):
        (out / f"sitemap-{n}.xml").write_text(urlset(ps), "utf-8")
    (out / "sitemap.xml").write_text(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        + "".join(f"<sitemap><loc>{html.escape(_BASE)}/sitemap-{n}.xml"
                  f"</loc></sitemap>" for n in range(1, len(parts) + 1))
        + "</sitemapindex>\n", "utf-8")


def _not_found_page() -> str:
    """Standalone 404 (GitHub Pages serves /404.html for any missing path).
    Self-contained: it renders at arbitrary depths, so no relative assets."""
    home = f"{_BASE}/" if _BASE else "./"
    return (
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Page not found – Xymon Archive</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;color:#1a1a1a;"
        "background:#fafafa;margin:0}main{max-width:560px;margin:14vh auto 0;"
        "padding:22px;text-align:center}h1{font-size:22px}a{color:#338a3a}"
        "</style></head><body><main><h1>Page not found</h1>"
        "<p>This page doesn't exist — the message may have moved when "
        "the archive was rebuilt.</p>"
        f"<p><a href='{home}'>Search the Xymon mailing list archive</a></p>"
        "</main></body></html>")


# The four index tabs (label, href) in display order; the Search tab is the
# site root. One list feeds the tab bar, the page writer AND the sitemap, so
# a new tab cannot be added to one and forgotten in another.
_TABS = (("Search", "index.html"), ("Threads", "index-latest.html"),
         ("Archive", "index-year.html"), ("Stats", "index-dashboard.html"))


def _load_names(conn) -> None:
    # map each (pseudonymised) sender address to its most-used display name, to
    # backfill messages whose From had only a bare address (see whom()).
    _NAMES.clear()
    _cnt: dict = {}
    for em, nm in conn.execute(
            "SELECT from_email, from_name FROM message "
            "WHERE from_name IS NOT NULL AND from_name <> '' "
            "AND from_name NOT LIKE '%@xymon.invalid' "
            "AND from_email IS NOT NULL AND from_email <> ''"):
        _cnt.setdefault(em, {})
        _cnt[em][nm] = _cnt[em].get(nm, 0) + 1
    for em, names in _cnt.items():
        _NAMES[em] = max(names, key=names.get)
