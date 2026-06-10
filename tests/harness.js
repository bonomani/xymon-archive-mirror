// Shared jsdom bootstrap for the behavioural tests (ui_test.js, fold_stats.js):
// one way to load the built site's client script, one way to swap globals per
// document, and ONE definition of a message's "visible" text -- so the
// regression assertions and the fold metrics can never measure two different
// notions of visibility.
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

// Full source of the built site's hashed client script.
function scriptSource(site) {
  const f = fs.readdirSync(site).find(x => /^script\..+\.js$/.test(x)) || 'script.js';
  return fs.readFileSync(path.join(site, f), 'utf8');
}

// Strip the DOMContentLoaded boot so the caller can eval() the definitions
// without them running against the placeholder document.
function stripBoot(src) {
  return src.replace(/document\.addEventListener\('DOMContentLoaded'[\s\S]*$/, '');
}

// The folders read the CURRENT global.document at call time, so tests just
// swap globals per document instead of re-evaluating the script (which leaks).
function setGlobals(dom) {
  global.document = dom.window.document; global.window = dom.window;
  global.NodeFilter = dom.window.NodeFilter; global.DOMParser = dom.window.DOMParser;
}

// Visible text of a message (.md/.pt body, or the element itself), skipping
// folded quotes (details.q): space-joined, whitespace-collapsed, trimmed.
function visText(el) {
  let v = '';
  (function w(x) { [].forEach.call(x.childNodes, n => {
    if (n.nodeType === 1 && n.classList && n.classList.contains('q')) return;
    if (n.nodeType === 3) v += ' ' + n.textContent; else if (n.nodeType === 1) w(n);
  }); })(el.querySelector('.md,.pt') || el);
  return v.replace(/\s+/g, ' ').trim();
}

module.exports = { JSDOM, scriptSource, stripBoot, setGlobals, visText };
