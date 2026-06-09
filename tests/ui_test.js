#!/usr/bin/env node
// Behavioural UI tests: run the generated client JS (script.js + the search
// page's inline script) against the BUILT site/ with jsdom and assert the
// behaviours we regressed on (folding, search, inline-expand). Exit non-zero on
// failure so CI blocks the deploy.  Run: SITE=site [FULL=1] [SCAN=500] node tests/ui_test.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const SITE = process.env.SITE || 'site';
const SCAN = parseInt(process.env.SCAN || '500', 10);   // cap thread scan (memory)
let fails = 0, skips = 0;
const ok   = m => console.log('  ok   ', m);
const fail = m => { console.error('  FAIL ', m); fails++; };
const skip = m => { console.log('  skip ', m); skips++; };
const assert = (c, m) => c ? ok(m) : fail(m);

const scriptFile = fs.readdirSync(SITE).find(f => /^script\..+\.js$/.test(f)) || 'script.js';
const script = fs.readFileSync(path.join(SITE, scriptFile), 'utf8');
const SC = script.replace(/document\.addEventListener\('DOMContentLoaded'[\s\S]*$/, '');

function setGlobals(dom) {
  global.document = dom.window.document; global.window = dom.window;
  global.NodeFilter = dom.window.NodeFilter; global.DOMParser = dom.window.DOMParser;
}
// define the client JS once (re-eval per doc would leak); folders read the
// CURRENT global.document at call time, so we just swap globals per thread.
setGlobals(new JSDOM('<!doctype html><body>', { url: 'https://x/' }));
eval(SC);

// fold one thread page; caller MUST close the returned dom to free memory.
function foldDoc(tid) {
  const dom = new JSDOM(fs.readFileSync(path.join(SITE, 'thread', tid + '.html'), 'utf8'));
  setGlobals(dom);
  foldQuotes(document);
  return dom;
}
function visText(m) {
  let v = '';
  (function w(el) { [].forEach.call(el.childNodes, n => {
    if (n.nodeType === 1 && n.classList && n.classList.contains('q')) return;
    if (n.nodeType === 3) v += n.textContent; else if (n.nodeType === 1) w(n);
  }); })(m.querySelector('.md,.pt'));
  return v.replace(/\s+/g, ' ').trim();
}

console.log('1) features present in script.js');
['function foldQuotes', 'function contentDedup', 'function loadMsg',
 'function loadThread', 'function hlTerms', 'function mergeAdjacent']
  .forEach(f => assert(script.includes(f), 'script.js defines ' + f));

console.log('1b) attribution colon is not split into the quote fold (NBSP glue)');
{
  // adjacent block elements give no separating space in textContent, so a French
  // attribution "a ecrit :" glues to the quoted word as one token ":Just".
  // contentDedup must fold from the WORD, leaving the colon on the attribution.
  const body = 'Just testing the email list here now.\n\nMore updates to share with you.';
  const d = new JSDOM('<!doctype html><body data-root="./">' +
    '<div class="tmsg"><div class="pt"><pre>' + body + '</pre></div></div>' +
    '<div class="tmsg"><div class="pt"><pre>Reply.\n\n' +
    'Le 1/1/2026, X a écrit :</pre><blockquote><pre>' + body +
    '</pre></blockquote></div></div></body>', { url: 'https://x/' });
  setGlobals(d);
  foldQuotes(document);
  const msg = document.querySelectorAll('.tmsg .pt')[1];
  const det = msg.querySelector('details.q');
  assert(det, 'reply quote is folded');
  const foldTxt = det ? det.textContent.replace(String.fromCharCode(0x25B8), '').trim() : '';
  const outer = det ? (msg.textContent || '').replace(det.textContent || '', '') : '';
  assert(outer.trim().slice(-1) === ':', 'attribution keeps its trailing colon (above the toggle)');
  assert(det && foldTxt.indexOf('Just') === 0, 'fold body starts at the quoted word, not the colon');
}

const idx = JSON.parse(fs.readFileSync(path.join(SITE, 'search-index.json'), 'utf8'));
const findMsg = (name, date) =>
  idx.find(r => (r[2] || '').includes(name) && (r[3] || '').startsWith(date));

console.log('2) folds carry a summary (no "Details" boxes) -- scan of the corpus');
{
  const all = [...new Set(idx.map(r => r[6]).filter(Boolean))];
  const tids = all.slice(0, SCAN);
  let folds = 0, empty = 0, errs = 0, done = 0;
  for (const tid of tids) {
    let dom;
    try { dom = foldDoc(tid); done++;
      document.querySelectorAll('details.q').forEach(x => {
        folds++; if (!x.querySelector(':scope>summary')) empty++; }); }
    catch (e) { errs++; }
    finally { if (dom) dom.window.close(); }
  }
  console.log(`   checked ${done}/${all.length} threads (cap ${SCAN}), ${folds} folds, ${errs} errors`);
  // anti "green-on-nothing": the run must actually exercise folding, and no
  // thread may throw (a broken eval/missing pages would otherwise pass at 0/0).
  assert(done > 0 && folds > 0, `folding exercised (${folds} folds / ${done} threads)`);
  assert(errs === 0, `no thread threw while folding (${errs} errors)`);
  assert(empty === 0, `summary-less folds = ${empty} / ${folds}`);
}

console.log('3) no over-fold: an author\'s own first/original reply stays visible');
// FULL=1 (set in CI on the complete build): a missing known case is a FAILURE,
// not a silent skip. Locally on a partial DB it stays a skip.
const missing = process.env.FULL === '1'
  ? m => fail(m + ' (FULL build: expected present)') : skip;
[['Matthew Goebel', '2026-01-15', 'I have not yet started using RHEL 10'],
 ['Becker Christian', '2026-03-02', 'BOOOOM']].forEach(([who, date, needle]) => {
  const r = findMsg(who, date);
  if (!r) return missing(`${who} ${date} not found in index`);
  let dom, m;
  try { dom = foldDoc(r[6]); m = document.getElementById('m-' + r[0]); } catch (e) {}
  if (!m) { if (dom) dom.window.close(); return missing(`${who} thread page missing`); }
  assert(visText(m).includes(needle), `${who}: reply "${needle}" stays visible`);
  dom.window.close();
});

console.log('4) search renders title + preview + expand toggle, and expands body-only + highlights');
{
  const home = fs.readFileSync(path.join(SITE, 'index.html'), 'utf8');
  const sm = home.match(/let DATA=\[\][\s\S]*?<\/script>/);
  if (!sm) { skip('no search script on index.html'); finish(); }
  else {
    const sjs = sm[0].replace('</script>', '');
    setGlobals(new JSDOM("<input id=q value='cert'><p id=stat></p><ul id=res></ul>" +
        "<input type=checkbox id=attonly>"));
    global.fetch = () => Promise.resolve({ text: () => Promise.resolve(
      "<div class=msg><h1>S</h1><p class=meta>A</p><div class=pt><pre>the cert expired here</pre></div></div>") });
    global.window.fetch = global.fetch;
    eval(SC);  // handler + loadMsg + hlTerms bound to this document
    const DATA = [["m1", "Cert issue", "Alice", "2026-01-10", 0, "t1", "t1"]];
    const BODIES = ["please renew the cert it expired"];
    eval(sjs.replace("let DATA=[], BODIES=null;",
      "let DATA=" + JSON.stringify(DATA) + ", BODIES=" + JSON.stringify(BODIES) + ";") + "\nrun();");
    const li = document.querySelector('#res>li');
    assert(li && li.querySelector('.tsub.xpand'), 'result line: subject is an expand toggle');
    assert(li && li.querySelector('.sprev .pline mark'), 'result line: preview highlights the term');
    if (li) {
      li.querySelector('.tsub').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      return Promise.resolve().then(() => new Promise(r => setTimeout(r, 30))).then(() => {
        const box = li.querySelector('.thmsgs');
        assert(box && !box.querySelector('h1') && !box.querySelector('p.meta'),
          'expanded message is body-only (no subject/meta)');
        assert(box && box.querySelector('mark'), 'expanded message highlights the term');
        assert(li.querySelector('.sprev').hidden, 'preview hidden while fully expanded');
        finish();
      });
    }
    finish();
  }
}

function finish() {
  console.log(`\n${fails ? 'FAILED' : 'OK'} — ${fails} failed, ${skips} skipped`);
  process.exit(fails ? 1 : 0);
}
