#!/usr/bin/env node
// Behavioural UI tests: run the generated client JS (script.js + the search
// page's inline script) against the BUILT site/ with jsdom and assert the
// behaviours we regressed on (folding, search, inline-expand). Exit non-zero on
// failure so CI blocks the deploy.  Run: SITE=site [FULL=1] [SCAN=500] node tests/ui_test.js
const fs = require('fs');
const path = require('path');
const { JSDOM, scriptSource, stripBoot, setGlobals, visText } =
  require('./harness');

const SITE = process.env.SITE || 'site';
const SCAN = parseInt(process.env.SCAN || '500', 10);   // cap thread scan (memory)
let fails = 0, skips = 0;
const ok   = m => console.log('  ok   ', m);
const fail = m => { console.error('  FAIL ', m); fails++; };
const skip = m => { console.log('  skip ', m); skips++; };
const assert = (c, m) => c ? ok(m) : fail(m);

const script = scriptSource(SITE);
const SC = stripBoot(script);

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

console.log('1) features present in script.js');
['function foldQuotes', 'function contentDedup', 'function loadMsg',
 'function loadThread', 'function hlTerms', 'function mergeAdjacent',
 'function lineEl']
  .forEach(f => assert(script.includes(f), 'script.js defines ' + f));

console.log('1a) page chrome: favicon, labelled search box, no leaked escapes, live year bars');
{
  const home = fs.readFileSync(path.join(SITE, 'index.html'), 'utf8');
  assert(home.includes('favicon.svg'), 'index.html links the favicon');
  assert(home.includes('aria-label="Search the archive"'), 'search input is labelled');
  const threads = fs.readFileSync(path.join(SITE, 'index-latest.html'), 'utf8');
  assert(!threads.split('<script>')[0].includes('\\u2026'),
    'Threads page text has no literal \\u2026 escape');
  const dash = fs.readFileSync(path.join(SITE, 'index-dashboard.html'), 'utf8');
  assert(dash.includes("href='index-year.html#y"),
    'Stats year bars link to the Archive page anchors');
  ['robots.txt', '404.html', 'favicon.svg'].forEach(f =>
    assert(fs.existsSync(path.join(SITE, f)), f + ' is generated'));
}

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

console.log('1c) contentDedup keeps an isolated inline re-quote visible (not folded)');
{
  // contentDedup is line-granular: a single duplicated line wedged between the
  // author's OWN lines is inline context (a quote they're replying to), so it must
  // stay visible and whole -- not folded as a fragment. Multi-line / bottom quotes
  // still fold (verified across the corpus in the section-2 scan below).
  const dup = 'documentation is still fragmented between sourceforge mailing lists ' +
    'distro patches wiki pages and github discussions';
  const prior = '<div class="tmsg"><div class="md"><p>' + dup +
    '</p><p>plus a separate original sentence here for padding.</p></div></div>';
  const reply = '<div class="tmsg"><div class="md"><div>On the initial post...<br>' +
    '<div>&gt;- ' + dup + ',</div><br><div>Is there anything new worth adding now.' +
    '</div></div></div></div>';
  const d = new JSDOM('<!doctype html><body data-root="./">' + prior + reply + '</body>',
    { url: 'https://x/' });
  setGlobals(d);
  foldQuotes(document);
  const msg = document.querySelectorAll('.tmsg .md')[1];
  let node = null;
  (function w(n) { [].forEach.call(n.childNodes, c => {
    if (c.nodeType === 3 && c.textContent.includes('still fragmented')) node = c;
    else if (c.nodeType === 1) w(c); }); })(msg);
  assert(node && !(node.parentElement && node.parentElement.closest('details.q')),
    'isolated inline re-quote stays visible (not folded into a fragment)');
}

const idx = JSON.parse(fs.readFileSync(path.join(SITE, 'search-index.json'), 'utf8'));
const findMsg = (name, date) =>
  idx.find(r => (r[2] || '').includes(name) && (r[3] || '').startsWith(date));

console.log('1d) folds never merge across messages (no summary-less "Détails" shells)');
{
  // Reproducer (thread db7b730b8752): a bottom quote in one message followed
  // by a short reply + French attribution in the NEXT message. mergeAdjacent
  // must not bridge the two -- a Range spanning two .tmsg blocks splits the
  // second <details> into a summary-less shell (browsers then show their
  // locale's default label, e.g. "Détails") and swallows the next message
  // into the previous fold.
  const q = '<blockquote><pre>orig text one two three four five six seven eight nine ten</pre></blockquote>';
  const m1 = '<details class=tmsg id=m-a open><summary>s1</summary>' +
    '<div class=pt><pre>Hi.</pre>' + q + '</div></details>';
  const m2 = '<details class=tmsg id=m-b open><summary>s2</summary>' +
    '<div class=pt><pre>Hello la liste! Bruno\n\nLe 09.06.2026 à 16:32, X a écrit :</pre>' +
    q + '</div></details>';
  const d = new JSDOM('<!doctype html><body data-root="./">' + m1 + m2 + '</body>',
    { url: 'https://x/' });
  setGlobals(d);
  foldQuotes(document);
  const tm = [...document.querySelectorAll('details.tmsg')];
  assert(tm.length === 2, 'two messages stay two <details.tmsg>');
  const all = [...document.querySelectorAll('details')];
  assert(all.every(t => t.querySelector(':scope>summary')),
    'no summary-less <details> shell (the "Détails" bug)');
  assert((tm[1] && tm[1].textContent || '').includes('Hello la liste'),
    'second message keeps its own content');
}

console.log('2) folds carry a summary (no "Details" boxes) -- scan of the corpus');
{
  const all = [...new Set(idx.map(r => r[6]).filter(Boolean))];
  const tids = all.slice(0, SCAN);
  let folds = 0, empty = 0, errs = 0, done = 0;
  for (const tid of tids) {
    let dom;
    try { dom = foldDoc(tid); done++;
      document.querySelectorAll('details').forEach(x => {   // .q AND .tmsg:
        if (x.classList.contains('q')) folds++;             // a split shell
        if (!x.querySelector(':scope>summary')) empty++; }); }   // = "Détails"
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

console.log('3b) one .sline row grammar on every surface (JS lineEl == month page markup)');
{
  const monthFile = fs.readdirSync(SITE)
    .find(f => /^\d{4}-[A-Z][a-z]+\.html$/.test(f));
  if (!monthFile) skip('no month page in site/');
  else {
    const mdom = new JSDOM(fs.readFileSync(path.join(SITE, monthFile), 'utf8'));
    const rows = [...mdom.window.document.querySelectorAll('.mlist li .sline')];
    assert(rows.length > 0, monthFile + ': month rows use the .sline grammar');
    const srv = rows.find(s => !s.querySelector('.clip')) || rows[0];
    const withClip = !!srv.querySelector('.clip');
    setGlobals(new JSDOM('<!doctype html><body>', { url: 'https://x/' }));
    const js = lineEl({ subject: 'S', author: 'A', when: '2026-01-01',
      att: withClip ? 1 : 0, mid: 'm1', href: 'msg/m1.html',
      threadHref: 'thread/t1.html#m-m1', msgHref: 'msg/m1.html' })
      .querySelector('.sline');
    const sig = el => [el.className,
      ...[...el.querySelectorAll('*')].map(x => x.tagName + '.' + x.className)]
      .join('|');
    assert(sig(js) === sig(srv),
      'lineEl and the server-side month row share one structure (' + sig(js) + ')');
    mdom.window.close();
  }
}

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
    eval(SC);  // handler + loadMsg + hlTerms + lineEl bound to this document
    eval(sjs); // defines the search page script + its window.__searchTest seam
    window.__searchTest.set(
      [["m1", "Cert issue", "Alice", "2026-01-10 09:00", 0, "t1", "t1"]],
      ["please renew the cert it expired"]);
    window.__searchTest.run();
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
