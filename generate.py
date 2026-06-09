#!/usr/bin/env python3
"""Generate a static mirror of the Xymon archive from SQLite.

Layout produced under ``site/``::

    index.html               years -> months with counts
    <YYYY-Month>.html        one month, messages by date (the only month view)
    thread/<tid>.html        one thread, all messages (a message's permalink is
                             thread/<tid>.html#m-<id>; there are no per-msg pages)
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from names import clean as _clean_name

from render_body import (  # body -> HTML rendering subsystem
    body_to_html, strip_footer, strip_scrub_notes, _QUOTE_PREFIX)
MONTH_ORDER = {
    m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)
}

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
.cnt{color:#888;font-size:12px}
.mgrid{display:flex;flex-wrap:wrap;gap:4px 16px;margin:4px 0 8px}
.mgrid a{text-decoration:none} .mgrid a.mgactive{font-weight:600;text-decoration:underline}
.mgrid span{color:#bbb}
.mpanel{margin:0 0 16px;padding:6px 0 0;border-top:1px solid #e3e3e3} .mpanel ul{margin:6px 0}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;
    color:#fff;background:#888;margin-right:6px;vertical-align:1px}
.badge.github{background:#24292f} .badge.list,.badge.imap{background:#338a3a}
.badge.inbox{background:#2e7d32}
.msg{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:8px 20px 16px}
.thread>p:last-child{display:none}      /* drop the old "<- index" footer on cached pages */
.tmsg{margin:14px 0}
.tmsg>summary{cursor:pointer;padding:4px 0;list-style:none}
.tmsg>summary::-webkit-details-marker{display:none}
.tmsg>summary::before{content:'\\25B8\\00A0';color:#338a3a;font-size:18px;vertical-align:-2px}
.tmsg[open]>summary::before{content:'\\25BE\\00A0'}
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
.msg>.md{border:0;border-radius:0;padding:0}
.pt pre{white-space:pre-wrap;background:transparent;border:0;border-radius:0;
    padding:0;margin:0;overflow:auto}
.pt blockquote{margin:6px 0 6px 1px;padding-left:10px;
    border-left:2px solid #ccc;color:#555}
.md{background:#fff;border:1px solid #e3e3e3;padding:14px;border-radius:6px}
.md pre{background:#f6f8fa} .md img{max-width:100%}
/* one rhythm everywhere: blocks have no margin, so spacing comes only from
   line-height and the single <br> blank lines kept by the sanitizer */
.md p,.md div,.md blockquote,.md table{margin:0}
.md blockquote{padding-left:10px;border-left:2px solid #ddd;color:#555}
ul.thread,ul.thread ul{list-style:none;padding-left:15px;
    border-left:1px solid #e8e8e8;margin:2px 0}
ul.thread li{margin:4px 0}
.att{margin:14px 0;padding:10px 14px;background:#fff7e6;
    border:1px solid #f0d9a8;border-radius:6px}
.att ul{margin:6px 0 0} .att li{margin:3px 0}
.hsearch{float:right;font-size:13px;opacity:.85}
.clip{font-size:12px;opacity:.75;cursor:default}
.tnav{display:flex;justify-content:space-between;gap:16px;font-size:13px;
    margin:10px 0;padding:6px 0;border-top:1px solid #eee;border-bottom:1px solid #eee}
.tnav a{text-decoration:none;color:#338a3a} .tnav .nx{margin-left:auto;text-align:right}
.tnav .lbl{color:#999}
pre a{color:#338a3a}
#q{width:100%;padding:9px 11px;font-size:15px;box-sizing:border-box;
    border:1px solid #ccc;border-radius:6px}
#res{list-style:none;padding:0} #res>li{margin:0;padding:7px 2px;border-bottom:1px solid #eee}
.sline{display:flex;align-items:baseline;gap:6px;font-size:14px}
.tsub{font-weight:600;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sline .meta{color:#888;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
#hq{width:100%;max-width:560px;margin:16px auto 4px;display:block;
    padding:12px 16px;font-size:16px;border:1px solid #ccc;border-radius:9px}
.hres{list-style:none;padding:0;max-width:620px;margin:6px auto}
.hres li{margin:6px 0}
.altlinks{text-align:center;font-size:13px;color:#888;margin:6px 0 18px}
.bars{margin:0}
.bar{display:grid;grid-template-columns:46px 1fr 60px;align-items:center;
    gap:9px;font-size:13px;margin:3px 0}
.bar .byr{color:#338a3a;text-decoration:none} .bar .bc{color:#888;text-align:right}
.ubar{display:grid;grid-template-columns:175px 1fr 56px;align-items:center;
    gap:9px;font-size:13px;margin:3px 0}
.ubar .un{color:#338a3a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ubar .bc{color:#888;text-align:right}
.stath{font-size:15px;font-weight:600;margin:22px 0 8px}
.bars .stath:first-child{margin-top:0}
.btrack{background:#e9f1e9;border-radius:3px}
.bbar{display:block;height:11px;background:#338a3a;border-radius:3px;min-width:2px}
.recent{margin:0;padding-left:2px;list-style:none}
.recent li{margin:6px 0}
.tbtn{font:inherit;color:#338a3a;background:#e9f1e9;border:1px solid #cfe0cf;
    border-radius:5px;padding:6px 14px;cursor:pointer}
.tbtn:hover{background:#dcebdc}
.thtoggle{cursor:pointer}
.thtoggle:hover{color:#338a3a}
.thtoggle::before{content:'\\25B8\\00A0';color:#338a3a;font-size:18px;vertical-align:-2px}
.thtoggle.thopen::before{content:'\\25BE\\00A0'}
.xpand{cursor:pointer}                        /* expand-in-place toggle (search/archive) */
.xpand::before{content:'\\25B8\\00A0';color:#338a3a;font-size:18px;vertical-align:-2px}
.xpand.thopen::before{content:'\\25BE\\00A0'}
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

# Linked (not inlined) -> behaviour tweaks need no page re-render. Folds every
# quoted block (<blockquote>) in a message body behind a toggle; runs on load
# and is called again after the Threads tab injects a thread inline.
SCRIPT = """
function foldQuotes(root){
  root=root||document;
  var KG=6;   // word-shingle size for content-dedup (declared early: used below)
  // summary = just an arrow (real char span, so a stale stylesheet never shows the
  // browser default "Details"); the .ar span rotates on open via CSS. No text label.
  function qsum(){ var s=document.createElement('summary');
    var a=document.createElement('span'); a.className='ar';
    a.textContent=String.fromCharCode(0x25B8); s.appendChild(a); return s; }
  root.querySelectorAll('blockquote').forEach(function(bq){
    if(bq.closest('details.q')) return;           // outermost quote only
    // merge consecutive quotes: if only whitespace/<br> sits before this one and
    // the node before that is an already-folded quote, append into it (one fold).
    var prev=bq.previousSibling;
    while(prev && ((prev.nodeType===3 && !prev.textContent.trim()) ||
                   (prev.nodeType===1 && prev.tagName==='BR'))) prev=prev.previousSibling;
    if(prev && prev.nodeType===1 && prev.tagName==='DETAILS' && prev.className==='q'){
      var n=bq.previousSibling, mid=[];
      while(n && n!==prev){ mid.unshift(n); n=n.previousSibling; }
      mid.forEach(function(x){ prev.appendChild(x); }); prev.appendChild(bq);
      return;
    }
    var d=document.createElement('details'); d.className='q';
    d.appendChild(qsum()); bq.parentNode.insertBefore(d,bq); d.appendChild(bq);
  });
  // Outlook/Exchange "forward" quote: a From:/Sent:/To:/Subject: header block
  // (no '>' markers) -> fold it and everything after. The header may be nested
  // (div>div>p) so find a COMPACT element holding all 3 fields anywhere, then
  // fold from its top-level block (direct child of the body) to the end.
  var FROM=/(^|[\\n\\s])(From|Von|De|Da|Van|Fra|Från)\\s*:/i;
  var SENT=/(Sent|Gesendet|Date|Envoy\\w*|Enviad\\w*|Inviat\\w*|Verzonden|Sendt|Skickat)\\s*:/i;
  var SUBJ=/(Subject|Betreff|Objet|Asunto|Oggetto|Onderwerp|Assunto|Emne|Ämne)\\s*:/i;
  // "----- Original Message -----" separator (specific multi-locale phrases, not a
  // generic dashed line -> a STRONG forward signal without false matches).
  var ORIG=/-{2,}\\s*(Original Message|Ursprüngliche Nachricht|Message d'origine|Mensaje original|Messaggio originale|Oorspronkelijk bericht|Oprindelig meddelelse|Ursprungligt meddelande|Opprinnelig melding)\\s*-{2,}/i;
  // confidentiality footer openers (multi-locale) -- kept OUT of the quote fold.
  var DISC=/(^|[\\n>])[ \\t]*(This (e-?mail|message|email|communication)\\b[^\\n]{0,60}\\b(intended|confidential|privileged|may contain|and any|contains|is meant)|CONFIDENTIAL(ITY)?\\b|The information (contained|in this|transmitted)|Diese E-?Mail\\b[^\\n]{0,50}(vertraulich|Empfänger)|Ce (courriel|message)\\b[^\\n]{0,50}(confidentiel|destin)|Este (mensaje|correo)\\b|LEGAL (NOTICE|DISCLAIMER)|NOTICE:)/i;
  // a single header-field line (From:/Sent:/To:/Subject:...) or an attribution line
  // ("On ... wrote:"); used to pull a quote's lead-in into a content-dedup fold.
  var HDR=/^[ \\t>]*(From|Von|De|Da|Van|Fra|Från|Sent|Gesendet|Date|Sendt|Skickat|To|An|Til|Cc|Bcc|Subject|Betreff|Objet|Emne|Ämne|Reply-To|Envoy\\w*|Enviad\\w*)\\b[^\\n]*:/i;
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
    var dets=[].slice.call(root.querySelectorAll('details.q'));
    for(var i=1;i<dets.length;i++){
      var a=dets[i-1], b=dets[i];
      if(!a.parentNode||!b.parentNode||a.contains(b)||b.contains(a)){ continue; }
      var rg=document.createRange(), gap;
      try{ rg.setStartAfter(a); rg.setEndBefore(b); gap=rg.toString(); }catch(e){ continue; }
      // merge if nothing real sits between -- whitespace, OR just an attribution /
      // header / separator line (it belongs to the next quote). A real reply -> keep.
      var g=gap.replace(/[\\s\\ufeff\\u200b]/g,'');
      if(g!=='' && !(gap.replace(/\\s+/g,' ').trim().length<150 &&
                     (ATTR.test(gap)||HDR.test(gap)||ORIG.test(gap)))) continue;
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
      if(visibleLen(body)<3) unwrap(d);   // never fold the WHOLE message (bottom-post)
    }catch(err){}
  }
  // visible (un-folded) text length of a body, ignoring details.q contents.
  function visibleLen(body){
    var t=0;(function w(el){[].forEach.call(el.childNodes,function(n){
      if(n.nodeType===1){ if(n.classList&&n.classList.contains('q')) return; w(n); }
      else if(n.nodeType===3) t+=n.textContent.replace(/[\\s\\ufeff]/g,'').length; });})(body);
    return t;
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
  function toks(text){   // normalized words with their raw text offsets
    var re=/\\S+/g, m, o=[];
    while((m=re.exec(text))){ var w=m[0].toLowerCase().replace(/[^a-z0-9]+/g,'');
      if(w) o.push({w:w, off:m.index, end:m.index+m[0].length}); }
    return o;
  }
  function shingles(t){ var s=[]; for(var i=0;i+KG<=t.length;i++){
    var g=''; for(var j=0;j<KG;j++) g+=t[i+j].w+' '; s.push(g); } return s; }
  function contentDedup(root){
    var bodies=root.querySelectorAll('.tmsg .pt,.tmsg .md');
    if(!bodies.length) bodies=root.querySelectorAll('.pt,.md');
    var prior={};
    [].forEach.call(bodies, function(body){
      var full=body.textContent||'', t=toks(full);
      if(t.length>=KG){
        var cov=new Array(t.length), i, j;
        for(i=0;i+KG<=t.length;i++){ var g=''; for(j=0;j<KG;j++) g+=t[i+j].w+' ';
          if(prior[g]) for(j=0;j<KG;j++) cov[i+j]=true; }
        var runs=[]; i=0;
        while(i<t.length){
          if(!cov[i]){ i++; continue; }
          var s=i, last=i, gap=0;                 // bridge small gaps (edited words)
          while(i<t.length){ if(cov[i]){ last=i; gap=0; i++; }
            else { if(++gap>KG) break; i++; } }
          if(last-s+1>=12) runs.push([s,last]);    // >=12 duplicated words
        }
        // fold exactly the duplicated words (no line-snap: HTML/flowed bodies have
        // no newlines in textContent, so snapping to a line would grab the whole
        // message and fold the author's own reply).
        for(var r=runs.length-1;r>=0;r--)
          foldRange(body, t[runs[r][0]].off, t[runs[r][1]].end);
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
    a.href=new URL(href,siteRoot()).href; a.innerHTML='&#128279;';  // link icon
    var c=document.createElement('button'); c.className='copytext'; c.type='button';
    c.innerHTML='&#128203;'; c.title=isThread?'copy whole thread':'copy this message';
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
// fetch a whole thread and inject it inline (Threads/Search/Archive expand in
// place). Rewrites ../ links since callers sit at the site root.
function loadThread(box,tid,fallback){
  box.innerHTML='<p class=meta>Loading\\u2026</p>';
  fetch('thread/'+tid+'.html').then(function(r){return r.text();}).then(function(t){
    var el=new DOMParser().parseFromString(t,'text/html').querySelector('.thread');
    if(!el){box.innerHTML="<p class=meta><a href='"+fallback+"'>open thread</a></p>";return;}
    el.querySelectorAll('h1,.thread>.meta').forEach(function(n){n.remove();});
    el.querySelectorAll('a[href$="index.html"]').forEach(function(a){
      var p=a.closest('p'); if(p&&p.parentNode===el)p.remove();});
    el.querySelectorAll('[href^="../"]').forEach(function(a){a.setAttribute('href',a.getAttribute('href').slice(3));});
    el.querySelectorAll('[src^="../"]').forEach(function(a){a.setAttribute('src',a.getAttribute('src').slice(3));});
    box.innerHTML=el.innerHTML;
    if(window.enhance)enhance(box); if(window.foldQuotes)foldQuotes(box);
    if(window.tidyBreaks)tidyBreaks(box);
  }).catch(function(){box.innerHTML="<p class=meta><a href='"+fallback+"'>open thread</a></p>";});
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
function loadMsg(box,mid,terms){
  box.innerHTML='<p class=meta>Loading\\u2026</p>';
  fetch('msg/'+mid+'.html').then(function(r){return r.text();}).then(function(t){
    var el=new DOMParser().parseFromString(t,'text/html').querySelector('.msg');
    if(!el){box.innerHTML='';return;}
    // body only -- the subject/author/date already show on the result/archive line
    el.querySelectorAll('h1,p.meta').forEach(function(n){n.remove();});
    el.querySelectorAll('[href^="../"]').forEach(function(a){a.setAttribute('href',a.getAttribute('href').slice(3));});
    el.querySelectorAll('[src^="../"]').forEach(function(a){a.setAttribute('src',a.getAttribute('src').slice(3));});
    box.innerHTML=el.innerHTML;
    if(window.enhance)enhance(box); if(window.foldQuotes)foldQuotes(box);
    if(window.tidyBreaks)tidyBreaks(box);
    hlTerms(box,terms);
  }).catch(function(){box.innerHTML='';});
}
// a link marked .xpand[data-mid] expands that single MESSAGE inline (search/archive);
// thread links navigate normally to the thread page.
document.addEventListener('click',function(e){
  var a=e.target.closest&&e.target.closest('.xpand[data-mid]'); if(!a) return;
  e.preventDefault();
  var host=a.closest('li')||a.parentNode;
  var prev=host.querySelector(':scope > .sprev');   // search preview (hide while full)
  var box=host.querySelector(':scope > .thmsgs');
  if(box){ box.hidden=!box.hidden; var open=!box.hidden;
    a.classList.toggle('thopen',open); if(prev) prev.hidden=open; return; }
  box=document.createElement('div'); box.className='thmsgs'; host.appendChild(box);
  a.classList.add('thopen'); if(prev) prev.hidden=true;
  var qel=document.getElementById('q');   // on the search page -> highlight the terms
  var terms=qel?qel.value.toLowerCase().split(/\\s+/).filter(Boolean):[];
  loadMsg(box,a.getAttribute('data-mid'),terms);
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

_SEARCH_BLURB = """<p class=meta>Search subject, sender and full message text
across __N__ messages.</p>"""

SEARCH_BODY = """
<h1>Search the archive</h1>
""" + _SEARCH_BLURB + """
<input id=q type=search placeholder="e.g. SSL certificate, or a sender name"
       autofocus autocomplete=off>
<p class=meta>
  <label><input type=checkbox id=attonly> &#128206; with attachments only</label>
</p>
<p id=stat class=meta>Loading search index&#8230;</p>
<ul id=res></ul>
<script>
let DATA=[], BODIES=null;
const q=document.getElementById('q'), res=document.getElementById('res'),
      stat=document.getElementById('stat'),
      attonly=document.getElementById('attonly');
Promise.all([
  fetch('search-index.json').then(r=>r.json()),
  fetch('body-index.json.gz').then(r=>
    new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).json())
]).then(([d,b])=>{DATA=d; BODIES=b;
  stat.textContent=d.length+' messages indexed.';
  const p=new URLSearchParams(location.search);   // topic chips land here
  if(p.has('q')) q.value=p.get('q');
  if(p.get('att')==='1') attonly.checked=true;
  run();})
 .catch(()=>{stat.textContent=
   'Search needs a modern browser (DecompressionStream).';});

function hl(text, terms){                  // bold the matched terms (safely)
  const frag=document.createDocumentFragment(), low=text.toLowerCase();
  let i=0;
  while(i<text.length){
    let at=-1, len=0;
    for(const t of terms){const p=low.indexOf(t,i);
      if(p>=0&&(at<0||p<at)){at=p;len=t.length;}}
    if(at<0){frag.appendChild(document.createTextNode(text.slice(i))); break;}
    if(at>i) frag.appendChild(document.createTextNode(text.slice(i,at)));
    const b=document.createElement('mark'); b.textContent=text.slice(at,at+len);
    frag.appendChild(b); i=at+len;
  }
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
function snipKey(sn){                       // normalize for de-duplication
  return sn.toLowerCase().replace(/[>\\s\\u2026]+/g,' ').trim();
}
function run(){
  if(!BODIES) return;                       // still loading the index
  const terms=q.value.toLowerCase().split(/\\s+/).filter(Boolean);
  const onlyAtt=attonly.checked;
  res.textContent='';
  if(!terms.length && !onlyAtt){
    stat.textContent=DATA.length+' messages indexed.'; return;}
  const hits=[];
  for(let i=0;i<DATA.length;i++){
    const m=DATA[i];
    if(onlyAtt && !m[4]) continue;
    const hay=(m[1]+' '+m[2]+' '+BODIES[i]).toLowerCase();
    if(terms.every(t=>hay.includes(t))) hits.push(i);
  }
  // flat list, newest first -- one dense line per matching message, date at the
  // end; clicking the line expands that message inline.
  hits.sort((a,b)=>(DATA[b][3]||'').localeCompare(DATA[a][3]||''));
  stat.textContent=hits.length+' match'+(hits.length==1?'':'es')+
    (onlyAtt?' with attachments':'');
  let shown=0;
  for(const i of hits){
    if(shown>=300) break; shown++;
    const m=DATA[i];
    const li=document.createElement('li');
    // line 1: subject = arrow + name, the whole thing toggles full <-> partial.
    const sl=document.createElement('div'); sl.className='sline';
    const a=document.createElement('a'); a.className='tsub xpand'; a.setAttribute('data-mid',m[0]);
    a.href='msg/'+m[0]+'.html';
    a.textContent=m[1]||'(no subject)';
    sl.appendChild(a);
    const au=document.createElement('span'); au.className='meta';   // author then date
    au.textContent=' \\u00b7 '+(m[2]||'')+' \\u00b7 '+(m[3]||'').slice(0,10); sl.appendChild(au);
    if(m[4]){const c=document.createElement('span'); c.className='clip';
      c.title=m[4]+' attachment'+(m[4]>1?'s':''); c.textContent='\\u{1F4CE}'; sl.appendChild(c);}
    const pl=document.createElement('span'); pl.className='plinks';   // permalinks (right)
    const lt=document.createElement('a'); lt.className='plink';
    lt.href=m[6]?'thread/'+m[6]+'.html#m-'+m[0]:'msg/'+m[0]+'.html';
    lt.textContent='\\u{1F9F5}'; lt.title='Open the thread'; pl.appendChild(lt);   // thread
    const lr=document.createElement('a'); lr.className='plink';
    lr.href='msg/'+m[0]+'.html'; lr.textContent='\\u{1F517}';   // raw message permalink
    lr.title='Permalink to the raw message'; pl.appendChild(lr);
    sl.appendChild(pl);
    li.appendChild(sl);
    // up to 6 preview lines: the text around each match (term highlighted), or
    // the start of the mail when the hit is only in the subject/sender.
    const prev=document.createElement('div'); prev.className='sprev';
    const sns=terms.length?snippets(BODIES[i],terms,6):[];
    if(sns.length){ for(const sn of sns){ const p=document.createElement('div');
      p.className='pline'; p.appendChild(hl(sn,terms)); prev.appendChild(p); } }
    else { const p=document.createElement('div'); p.className='pline';
      p.textContent=(BODIES[i]||'').slice(0,200); prev.appendChild(p); }
    li.appendChild(prev);
    res.appendChild(li);
  }
}
let t; q.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(run,200);});
attonly.addEventListener('change',run);
</script>
"""


# Content-hashed asset names: the URL changes only when the file changes, so the
# browser/CDN always pick up a new version (no stale cache, no hard-refresh)
# while unchanged assets stay cached forever.
_JS_NAME = f"script.{hashlib.sha1(SCRIPT.encode()).hexdigest()[:10]}.js"
_CSS_NAME = f"style.{hashlib.sha1(CSS.encode()).hexdigest()[:10]}.css"


def page(title: str, body: str, root: str = "", header: bool = True,
         scripts: bool = True) -> str:
    # root is "" for top-level pages and "../" for pages under msg/, so the
    # header links resolve from any depth. header=False omits the top bar (the
    # home page carries the title+search in its hero, so the bar is redundant).
    # scripts=False -> a canonical, feature-free page (no fold/copy JS).
    bar = (f"<header><a href={root}index.html>Xymon Mailing List Archive</a> "
           f"<a class=hsearch href={root}index.html>search</a></header>"
           if header else "")
    js = (f"<script src='{root}{_JS_NAME}' defer></script>" if scripts else "")
    return (
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<link rel=stylesheet href='{root}{_CSS_NAME}'>{js}</head>"
        f"<body data-root='{root}'>{bar}<main>{body}</main></body></html>"
    )


def e(s: str | None) -> str:
    return html.escape(s or "")


def month_key(month: str) -> tuple[int, int]:
    try:
        year, name = month.split("-", 1)
        return (int(year), MONTH_ORDER.get(name, 0))
    except ValueError:
        return (0, 0)


def _sortkey(r):
    return (r["date_iso"] is None, r["date_iso"] or "", r["id"])


def short_date(r) -> str:
    """Compact list date: 2024-07-05 12:32 (full original kept on msg page)."""
    iso = r["date_iso"]
    if iso:
        return iso[:16].replace("T", " ")
    return r["date_raw"] or ""


def _clip(n: int) -> str:
    if not n:
        return ""
    return (f" <span class=clip title='{n} attachment"
            f"{'s' if n > 1 else ''}'>&#128206;</span>")


def msg_name(r) -> str:
    """Stable message-page stem: a hash of the (globally unique) Message-Id.

    The SQLite rowid churns whenever a month is re-crawled (DELETE+INSERT), so
    using it would break permalinks every CI run. The msgid hash is permanent.
    """
    mid = r["msgid"]
    if mid:
        return hashlib.sha1(mid.encode("utf-8", "replace")).hexdigest()[:16]
    return f"x{r['id']}"


def _href(r, root: str = "") -> str:
    """Link target for a message line (root-relative): its thread page anchored
    to the message when a stable thread_id exists, else the message permalink."""
    tid = r["thread_id"] if "thread_id" in r.keys() else None
    return (f"{root}thread/{tid}.html#m-{msg_name(r)}" if tid
            else f"{root}msg/{msg_name(r)}.html")


def _xp(r) -> str:
    """Make the whole subject link an expand-in-place MESSAGE toggle (arrow + name);
    the global handler injects msg/<id>.html inline. href is the no-JS fallback."""
    return f"class=xpand data-mid='{msg_name(r)}' href='msg/{msg_name(r)}.html'"


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


def render_threads(rows, att_counts=None) -> str:
    """Render a month's rows as a nested reply tree (Pipermail thread view).

    A message links under its ``in_reply_to`` parent when that parent is in the
    same month; otherwise it is a thread root (covers cross-month replies and
    missing parents). Ordered by date within each level; a per-branch seen-set
    guards against malformed reply cycles.
    """
    att_counts = att_counts or {}
    by_msgid = {r["msgid"]: r for r in rows if r["msgid"]}
    children: dict[str, list] = defaultdict(list)
    has_parent = set()
    for r in rows:
        parent = r["in_reply_to"]
        if parent and parent in by_msgid and parent != r["msgid"]:
            children[parent].append(r)
            has_parent.add(r["id"])

    # Define threads by union-find over reply links AND a shared distinctive
    # subject (so a subject-changed reply and its scattered siblings end up in
    # one tree -- not split). Same grouping as thread_nav, kept consistent.
    uf = {r["id"]: r["id"] for r in rows}

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            uf[ra] = rb

    for r in rows:
        p = r["in_reply_to"]
        if p and p in by_msgid and p != r["msgid"]:
            union(r["id"], by_msgid[p]["id"])
    subj_first: dict[str, int] = {}
    for r in rows:
        s = (r["subject"] or "").strip().lower()
        if len(s) > 8:
            if s in subj_first:
                union(r["id"], subj_first[s])
            else:
                subj_first[s] = r["id"]

    comp: dict[int, list] = defaultdict(list)
    for r in rows:
        comp[find(r["id"])].append(r)

    # One display root per thread (earliest true root); attach the component's
    # other roots (missing/foreign parents) under it so the thread is one tree.
    roots = []
    for members in comp.values():
        in_roots = sorted((m for m in members if m["id"] not in has_parent),
                          key=_sortkey)
        if not in_roots:                       # cycle: fall back to earliest
            in_roots = [min(members, key=_sortkey)]
        primary = in_roots[0]
        roots.append(primary)
        if primary["msgid"]:
            for m in in_roots[1:]:
                children[primary["msgid"]].append(m)

    def node(r, seen: frozenset) -> str:
        who = whom(r)
        out = (f"<li><a {_xp(r)}>"
               f"{e(r['subject']) or '(no subject)'}</a>"
               f"{_clip(att_counts.get(r['msgid'], 0))} "
               f"<span class=meta>{e(who)} &middot; {short_date(r)}</span>")
        kids = sorted(children.get(r["msgid"], ()), key=_sortkey)
        if kids and r["id"] not in seen:
            seen = seen | {r["id"]}
            out += "<ul>" + "".join(node(k, seen) for k in kids) + "</ul>"
        return out + "</li>"

    roots.sort(key=_sortkey)
    return ("<ul class=thread>"
            + "".join(node(r, frozenset()) for r in roots) + "</ul>")


_ARCHIVE_JS = """
<script>
function loadFrag(panel, frag){
  panel.dataset.frag = frag;
  panel.innerHTML = '<p class=meta>Loading\\u2026</p>';
  fetch('frag/' + encodeURIComponent(frag) + '.html').then(function(r){ return r.text(); })
    .then(function(h){ if (panel.dataset.frag === frag) panel.innerHTML = h; })
    .catch(function(){ panel.innerHTML = '<p class=meta>Failed to load.</p>'; });
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
<p id=tstat class=meta>Loading threads\\u2026</p>
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
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  var load=window.loadThread;   // shared inline-thread loader (defined in script.js)
  function addThread(idx){
    var f=D[idx[0]],last=(D[idx[idx.length-1]][3]||'').slice(0,10),n=idx.length,tid=f[6];
    var fb=tid?'thread/'+tid+'.html':'msg/'+f[0]+'.html',loaded=false;
    var li=document.createElement('li');
    var tg=document.createElement('div');tg.className='thtoggle';
    tg.innerHTML=esc(f[1]||'(no subject)')+' <span class=meta>&middot; '+esc(f[2])+
      ' &middot; '+last+' &middot; '+n+' message'+(n>1?'s':'')+'</span>';
    var box=document.createElement('div');box.className='thmsgs';box.hidden=true;
    tg.addEventListener('click',function(){
      this.classList.toggle('thopen');box.hidden=!box.hidden;
      if(!box.hidden&&!loaded){loaded=true;load(box,tid,fb);}
    });
    li.appendChild(tg);li.appendChild(box);list.appendChild(li);
  }
  function batch(){
    var end=Math.min(shown+B,G.length);
    for(;shown<end;shown++) addThread(G[shown]);
    more.hidden=shown>=G.length;
  }
  more.addEventListener('click',batch);batch();
}).catch(function(){document.getElementById('tstat').textContent='Could not load threads (needs a modern browser).';});
</script>
"""


# Folded into every message-page signature: bump when the RENDERING changes
# (not the data), so the incremental manifest re-renders all pages once.
RENDER_VERSION = "9-faithful-ol"


def build(db: Path, out: Path) -> None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    has_tid = "thread_id" in {r[1] for r in
                              conn.execute("PRAGMA table_info(message)")}

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

    out.mkdir(parents=True, exist_ok=True)
    (out / "msg").mkdir(exist_ok=True)   # canonical single-message permalink pages
    (out / "att").mkdir(exist_ok=True)
    (out / "frag").mkdir(exist_ok=True)
    (out / _CSS_NAME).write_text(CSS, "utf-8")       # content-hashed names so the
    (out / _JS_NAME).write_text(SCRIPT, "utf-8")     #   browser/CDN never serve a
    #   stale asset; the <link>/<script> hrefs carry the same hash (see page()).
    for old in out.glob("style.*.css"):              # drop superseded hashed assets
        if old.name != _CSS_NAME: old.unlink()
    for old in out.glob("script.*.js"):
        if old.name != _JS_NAME: old.unlink()
    for bare in ("style.css", "script.js"):          # drop pre-hash leftovers (old cache)
        if (out / bare).exists(): (out / bare).unlink()
    # drop the obsolete per-month sort variants (the date/threaded/author switcher
    # was removed -- only {m}.html / frag/{m}.html remain). Stale files survive in
    # the cache-restored site/, so prune them so they don't linger on the deploy.
    for sub in (out, out / "frag"):
        for pat in ("*-date.html", "*-author.html", "*-thread.html"):
            for old in sub.glob(pat):
                old.unlink()

    # attachments grouped by their message (msgid)
    atts_by_msgid: dict[str, list] = defaultdict(list)
    for a in conn.execute(
            "SELECT id, msgid, filename, content_type, size, content "
            "FROM attachment WHERE msgid IS NOT NULL"):
        atts_by_msgid[a["msgid"]].append(a)
    att_counts = {mid: len(v) for mid, v in atts_by_msgid.items()}
    att_msg_per_month = dict(conn.execute(
        "SELECT month, COUNT(DISTINCT msgid) FROM attachment "
        "WHERE msgid IS NOT NULL GROUP BY month"))

    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT month FROM message")]
    months.sort(key=month_key, reverse=True)

    # ---- index: years -> months
    counts = {m: conn.execute(
        "SELECT COUNT(*) FROM message WHERE month=?", (m,)).fetchone()[0]
        for m in months}
    years: dict[str, list[str]] = {}
    for m in months:
        years.setdefault(m.split("-", 1)[0], []).append(m)

    total = sum(counts.values())
    yrs = sorted(years)
    span = f"{yrs[0]}–{yrs[-1]}" if yrs else ""

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
        c = yr_count[y]
        bars += (f"<div class=bar><a class=byr href='#y{y}'>{e(y)}</a>"
                 f"<span class=btrack><span class=bbar "
                 f"style='width:{round(100*c/maxy)}%'></span></span>"
                 f"<span class=bc>{c:,}</span></div>")
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
        bars += (f"<div class=ubar><span class=un>{e(nm)}</span>"
                 f"<span class=btrack><span class=bbar "
                 f"style='width:{round(100*c/maxu)}%'></span></span>"
                 f"<span class=bc>{c:,}</span></div>")
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
    _LAYOUTS = [("Search", "index.html", ""),
                ("Threads", "index-latest.html", recent),
                ("Archive", "index-year.html", grid),
                ("Stats", "index-dashboard.html", bars)]

    def tabs(current):
        parts = [(f"<b>{lbl}</b>" if lbl == current
                  else f"<a href='{href}'>{lbl}</a>")
                 for lbl, href, _ in _LAYOUTS]
        return "<p class=altlinks>" + " | ".join(parts) + "</p>"

    # the full search widget, minus its own <h1> and the descriptive blurb --
    # the hero already sets the scene, so the box sits clean right under it.
    widget = (SEARCH_BODY
              .replace("<h1>Search the archive</h1>", "")
              .replace(_SEARCH_BLURB, "")
              .replace("__N__", str(total)))

    for lbl, href, section in _LAYOUTS:
        # the search box lives only on the Search tab, not on Archive / Stats
        mid = widget if href == "index.html" else ""
        (out / href).write_text(
            page("Xymon Archive", hero + tabs(lbl) + mid + section,
                 header=False), "utf-8")

    # thread id per message (reply links + shared distinctive subject), so the
    # client can group search hits under their thread.
    trows = conn.execute(
        "SELECT id, msgid, in_reply_to, subject FROM message").fetchall()
    tby = {r["msgid"]: r["id"] for r in trows if r["msgid"]}
    tpar = {r["id"]: r["id"] for r in trows}

    def tfind(x):
        while tpar[x] != x:
            tpar[x] = tpar[tpar[x]]
            x = tpar[x]
        return x

    def tunion(a, b):
        ra, rb = tfind(a), tfind(b)
        if ra != rb:
            tpar[ra] = rb

    for r in trows:
        irt = r["in_reply_to"]
        if irt and irt in tby:
            tunion(r["id"], tby[irt])
    tsubj = {}
    for r in trows:
        s = (r["subject"] or "").strip().lower()
        if len(s) > 8:
            if s in tsubj:
                tunion(r["id"], tsubj[s])
            else:
                tsubj[s] = r["id"]
    roots, tid_of = {}, {}
    for r in trows:
        root = tfind(r["id"])
        tid_of[r["id"]] = roots.setdefault(root, len(roots))

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
            (r["date_iso"] or "")[:10], att_counts.get(r["msgid"], 0),
            tid_of[r["id"]],                       # [5] grouping key (per build)
            r["thread_id"] if has_tid else ""])    # [6] stable thread/<tid> link
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
    (out / "body-index.json.gz").write_bytes(gzip.compress(
        json.dumps(bidx, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8"), 9))

    # ---- incremental change detection (Phase 1): the ~tens-of-thousands of
    # per-message pages dominate the build. With INCREMENTAL=1 and a previous
    # site/.manifest.json (restored from CI cache), only pages whose input
    # changed are re-rendered; the rest are kept from the cached site. A page's
    # Incremental manifest, keyed by THREAD. There are no per-message pages: a
    # message's permalink is thread/<tid>.html#m-<id>. A thread re-renders when
    # any member's content (or RENDER_VERSION) changes; the thread sigs are
    # computed in the thread pass below. No manifest / INCREMENTAL unset -> full.
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

    # ---- per-month pages (the per-message pages were dropped; see thread pass)
    for m in months:
        rows = conn.execute(
            f"""SELECT id, msgid, in_reply_to, subject, from_name, from_email,
                      date_raw, date_iso{', thread_id' if has_tid else ''}
               FROM message WHERE month=?
               ORDER BY date_iso IS NULL, date_iso, id""", (m,)).fetchall()

        def flat_list(ordered) -> str:
            out_ = ""
            for r in ordered:
                who = whom(r)
                out_ += (
                    f"<li><a {_xp(r)}>"
                    f"{e(r['subject']) or '(no subject)'}</a>"
                    f"{_clip(att_counts.get(r['msgid'], 0))} "
                    f"<span class=meta>{e(who)} &middot; "
                    f"{short_date(r)}</span></li>")
            return f"<ul class=mlist>{out_}</ul>"

        # Single view: messages by date. The sort switcher (date / threaded /
        # by author) was removed -- only date remains. The threaded view of a
        # conversation still lives on its own thread/<id>.html page.
        content = flat_list(rows)

        # downloadable mbox: original message bytes in date order
        raws = [rb for (rb,) in conn.execute(
            "SELECT raw FROM message WHERE month=? AND raw IS NOT NULL "
            "ORDER BY date_iso IS NULL, date_iso, id", (m,)) if rb]
        mbox_link = ""
        if raws:
            mbox = b"".join(rb if rb.endswith(b"\n") else rb + b"\n"
                            for rb in raws)
            (out / f"{m}.txt.gz").write_bytes(gzip.compress(mbox, 9))
            mbox_link = f" &middot; <a href='{e(m)}.txt.gz'>mbox.gz</a>"

        nav = (f"<p class=meta>{len(rows)} messages{mbox_link} &middot; "
               f"<a href='index.html'>&larr; index</a></p>")
        mbody = f"<h1>{e(m)}</h1>{nav}{content}{nav}"
        (out / f"{m}.html").write_text(page(f"Xymon {m}", mbody), "utf-8")
        # accordion fragment (loaded by the month index); msg/<id>.html links
        # resolve when the fragment is injected at the site root.
        (out / "frag" / f"{m}.html").write_text(
            f"<p class=meta>{len(rows)} messages</p>{content}", "utf-8")

    # ---- thread pages: ONE page per thread, every message in order, each a
    # collapsible <details> block anchored #m-<id> (that anchor IS the message
    # permalink -- there are no per-message pages). Carries the full body,
    # author/date/source and the message's attachments. Incremental: a thread
    # re-renders only when its content signature (or RENDER_VERSION) changes.
    (out / "thread").mkdir(exist_ok=True)
    bythread: dict = defaultdict(list)
    for r in conn.execute("SELECT * FROM message").fetchall():
        key = (r["thread_id"] if has_tid and r["thread_id"] else msg_name(r))
        bythread[key].append(r)

    def _att_block(r):
        """Write this message's attachment files and return the HTML box."""
        atts = atts_by_msgid.get(r["msgid"], ())
        if not atts:
            return ""
        links = ""
        for a in atts:
            fname = _safe(a["filename"])
            adir = out / "att" / str(a["id"])
            adir.mkdir(parents=True, exist_ok=True)
            (adir / fname).write_bytes(a["content"])
            links += (f"<li><a href='../att/{a['id']}/{e(fname)}'>{e(fname)}</a> "
                      f"<span class=meta>{e(a['content_type'] or '')} &middot; "
                      f"{_human(a['size'])}</span></li>")
        return (f"<div class=att><b>Attachments ({len(atts)})</b>"
                f"<ul>{links}</ul></div>")

    new_threads, nth = {}, 0
    for tid, members in bythread.items():
        members.sort(key=lambda r: (r["date_iso"] is None,
                                    r["date_iso"] or "", r["id"]))
        sig = hashlib.blake2b(("\x00".join(
            [RENDER_VERSION] +
            ["\x1f".join([msg_name(r), r["subject"] or "", whom(r),
                          r["from_email"] or "", r["date_raw"] or "",
                          r["source"] or "", r["body"] or "", r["body_html"] or "",
                          ";".join(f"{a['id']}:{a['size']}"
                                   for a in atts_by_msgid.get(r["msgid"], ()))])
             for r in members])).encode("utf-8", "replace"),
            digest_size=16).hexdigest()
        new_threads[tid] = sig
        if (incremental and old_threads.get(tid) == sig
                and (out / "thread" / f"{tid}.html").exists()):
            continue
        head = members[0]
        blocks = []
        for r in members:
            anchor = msg_name(r)
            src = r["source"] or "list"
            badge = f"<span class='badge {e(src)}'>{e(src)}</span>"
            email = (f" &lt;{e(r['from_email'])}&gt;"
                     if (r["from_email"] and "@" in r["from_email"]
                         and not r["from_email"].endswith("@xymon.invalid"))
                     else "")
            mbody = body_to_html(r["body"], r["body_html"])
            matts = _att_block(r)
            # thread block (foldable, threaded view). The copy marker's data-href
            # is the MESSAGE permalink -> its canonical msg/<id>.html page.
            blocks.append(
                f"<details class=tmsg id=m-{anchor} open>"
                f"<summary>{badge} <b>{e(whom(r))}</b>{email} "
                f"<span class=meta>&middot; {e(r['date_raw'])} &middot; "
                f"<button class=copy type=button data-href='msg/{anchor}.html'"
                f" title='copy link to this message'>&#128279; link</button>"
                f"</span></summary>{mbody}{matts}</details>")
            # canonical single-message page: full body, quotes NOT folded, no JS
            # (scripts=False) -> a stable, feature-free reference for permalinks.
            msg_html = (
                f"<div class=msg><h1>{e(r['subject']) or '(no subject)'}</h1>"
                f"<p class=meta>{badge} <b>{e(whom(r))}</b>{email}"
                f"<br>{e(r['date_raw'])}"
                f"{'<br>Message-Id: ' + e(r['msgid']) if r['msgid'] else ''}</p>"
                f"{mbody}{matts}</div>")
            (out / "msg" / f"{anchor}.html").write_text(
                page(e(r["subject"]) or "message", msg_html, root="../",
                     scripts=False), "utf-8")
        tbody = (f"<div class=thread>"
                 f"<h1>{e(head['subject']) or '(no subject)'} "
                 f"<button class=copy type=button data-href='thread/{e(tid)}.html'"
                 f" title='copy link to this thread'>&#128279; link</button></h1>"
                 f"<p class=meta>{len(members)} message"
                 f"{'s' if len(members) != 1 else ''} in this thread</p>"
                 + "".join(blocks) + "</div>")
        (out / "thread" / f"{tid}.html").write_text(
            page(e(head["subject"]) or "thread", tbody, root="../"), "utf-8")
        nth += 1
    print(f"{'incremental' if incremental else 'full'} render: "
          f"{nth}/{len(bythread)} thread pages")

    # drop pages for threads that no longer exist, then persist the manifest.
    for gone in set(old_threads) - set(new_threads):
        stale = out / "thread" / f"{gone}.html"
        if stale.exists():
            stale.unlink()
    manifest_path.write_text(
        json.dumps({"threads": new_threads, "assets": [_JS_NAME, _CSS_NAME]},
                   separators=(",", ":")), "utf-8")

    conn.close()
    print(f"Generated site in {out}/ ({len(months)} months)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate static Xymon mirror")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--out", default="site", type=Path)
    args = ap.parse_args()
    build(args.db, args.out)


if __name__ == "__main__":
    main()
