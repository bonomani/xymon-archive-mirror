#!/usr/bin/env node
// Measurement (not a pass/fail test): run the real folding over a sample of the
// built corpus and report numbers, so "robust" stops being a guess.
//   SITE=site SAMPLE=400 node tests/fold_stats.js
// Reports: folds, summary-less, folds opened by maybeOpen, folds whose content
// is NOT found earlier in the thread (upper bound on false positives -- includes
// legit off-thread quotes), and visible text that IS duplicated from an earlier
// message (missed-dup / false-negative signal). Prints samples for human review.
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { JSDOM, scriptSource, stripBoot, setGlobals, visText } =
  require('./harness');

const SITE = process.env.SITE || 'site';
const SAMPLE = parseInt(process.env.SAMPLE || '400', 10);
const DATA = JSON.parse(fs.readFileSync(path.join(SITE, 'search-index.json'), 'utf8'));
const BODIES = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(SITE, 'body-index.json.gz'))));
const SC = stripBoot(scriptSource(SITE));

const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(/\s+/).filter(Boolean);
const byTid = {};
DATA.forEach((r, i) => { if (r[6]) (byTid[r[6]] = byTid[r[6]] || []).push(i); });
function addShingles(words, k, set) { for (let i = 0; i + k <= words.length; i++) set.add(words.slice(i, i + k).join(' ')); }
function longestCoveredRun(words, prior, k) {
  const cov = new Array(words.length).fill(false);
  for (let i = 0; i + k <= words.length; i++)
    if (prior.has(words.slice(i, i + k).join(' '))) for (let j = i; j < i + k; j++) cov[j] = true;
  let run = 0, mx = 0; for (const c of cov) { run = c ? run + 1 : 0; if (run > mx) mx = run; } return mx;
}
setGlobals(new JSDOM('<!doctype html><body>'));   // define client JS once
eval(SC);

const tids = [...new Set(DATA.map(r => r[6]).filter(Boolean))];
const sample = tids.slice(0, SAMPLE);
let threads = 0, msgs = 0, folds = 0, empty = 0, opened = 0, suspect = 0, missed = 0;
const suspects = [], misses = [];

for (const tid of sample) {
  let html; try { html = fs.readFileSync(path.join(SITE, 'thread', tid + '.html'), 'utf8'); } catch (e) { continue; }
  threads++;
  const dom = new JSDOM(html);
  setGlobals(dom);
  foldQuotes(document);
  const members = byTid[tid].slice().sort((a, b) => (DATA[a][3] || '').localeCompare(DATA[b][3] || ''));
  const prior = new Set();
  for (const i of members) {
    const el = document.getElementById('m-' + DATA[i][0]);
    if (el) {
      msgs++;
      el.querySelectorAll('details.q').forEach(d => {
        folds++;
        if (!d.querySelector(':scope>summary')) empty++;
        if (d.open) opened++;
        const fw = norm(d.textContent);
        if (fw.length >= 8 && longestCoveredRun(fw, prior, 6) < 8) {
          suspect++; if (suspects.length < 12) suspects.push(`[${DATA[i][2]}] ${fw.slice(0, 18).join(' ')}…`);
        }
      });
      const vw = norm(visText(el));
      if (vw.length >= 12 && longestCoveredRun(vw, prior, 6) >= 12) {
        missed++; if (misses.length < 12) misses.push(`[${DATA[i][2]}] tid=${tid} mid=${DATA[i][0]}`);
      }
    }
    // build `prior` from the RENDERED message text (same domain as the folds), so
    // the "not found earlier" metric stops over-counting legit quotes that the
    // indexed body normalises differently. Fall back to the index if no element.
    addShingles(norm(el ? (el.querySelector('.md,.pt') || el).textContent : BODIES[i]), 6, prior);
  }
  dom.window.close();   // free the jsdom tree (else the sample OOMs)
}

const pct = (a, b) => b ? (100 * a / b).toFixed(1) + '%' : 'n/a';
console.log(`\n=== fold measurement (sample ${threads}/${tids.length} threads, ${msgs} messages) ===`);
console.log(`folds total            : ${folds}`);
console.log(`  summary-less         : ${empty}  (must be 0)`);
console.log(`  opened by maybeOpen  : ${opened}  (${pct(opened, folds)})`);
console.log(`  not found earlier    : ${suspect}  (${pct(suspect, folds)})  <- FP upper bound (incl. legit off-thread quotes)`);
console.log(`messages w/ missed dup : ${missed}  (${pct(missed, msgs)})  <- duplicated text left visible (FN signal)`);
console.log(`\nsample "not found earlier" folds:`); suspects.forEach(s => console.log('  - ' + s));
console.log(`\nsample missed-dup messages:`); misses.forEach(s => console.log('  - ' + s));
