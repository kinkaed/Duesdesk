import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../work.tsx', import.meta.url), 'utf8');

const css = fs.readFileSync(new URL('../styles.css', import.meta.url), 'utf8');
// Comments are stripped first: otherwise a doc comment sitting above a rule is
// absorbed into that rule's selector and the rule becomes invisible to matching.
const sheet = css.replace(/\/\*[\s\S]*?\*\//g, '');
// All rules in the sheet, as selector -> body. Matching a whole rule rather than
// a substring keeps a media query from satisfying a test about the base sheet.
const rules = () => [...sheet.matchAll(/(^|[\n};])\s*([^{}\n][^{}]*?)\s*\{([^}]*)\}/g)]
  .map(m => ({ selectors: m[2].split(',').map(s => s.trim()), body: m[3].trim() }));
const matching = selector => rules().filter(r => r.selectors.includes(selector));

test('the profile entry is styled by the same rules as the nav buttons', () => {
  // Every state a nav button has, the profile entry must have too, in one rule
  // that names both. If any of these names only .user-block, the profile entry
  // has its own palette and will drift out of the organization branding.
  ['.nav.active', '.nav:not(.active):hover', '.nav:not(.active)'].forEach(selector => {
    const owner = selector.replace('.nav', '');
    matching(selector).forEach(rule => {
      const twin = rule.selectors.includes('.user-block' + owner);
      const alsoNav = rule.selectors.includes('.nav' + owner);
      assert.ok(!alsoNav || twin, `rule for "${selector}" styles .nav but not .user-block: ${rule.selectors.join(', ')}`);
    });
  });
  // And the themed block at the end must re-theme the profile entry with the nav.
  ['.nav.active', '.nav:not(.active):hover'].forEach(selector => {
    const owner = selector.replace('.nav', '');
    const themed = matching(selector).filter(r => r.body.includes('--org-'));
    themed.forEach(rule => {
      assert.ok(rule.selectors.includes('.user-block' + owner),
        `themed rule "${rule.selectors.join(', ')}" does not include .user-block${owner}`);
    });
    assert.ok(themed.length, `${selector} has no organization-themed rule`);
  });
});

test('the sidebar shares one focus ring, and it is visible on the brand colour', () => {
  const rings = matching('.sidebar button:focus-visible');
  assert.ok(rings.length >= 1, 'the sidebar needs its own focus ring');
  // Exactly one rule may define the ring itself; later ones may only recolour
  // it, the same way the branding block recolours .nav.active.
  const defining = rings.filter(r => /(^|;)\s*outline\s*:/.test(r.body));
  assert.equal(defining.length, 1, 'sidebar buttons must not define competing focus rings');
  assert.match(defining[0].body, /var\(--lime\)|var\(--org-secondary\)/,
    'the default global ring is dark green and invisible on a dark branded sidebar');
  rings.filter(r => r !== defining[0]).forEach(r => {
    assert.doesNotMatch(r.body, /outline-offset|outline-width|outline-style/,
      'only the colour may be re-themed, so nav and profile focus identically');
  });
  // The global rule must not win for sidebar buttons, or the ring reverts to dark.
  assert.ok(rings.length >= 1 && !matching('button:focus-visible').some(r => r.selectors.includes('.sidebar button:focus-visible')));
});

test('the fixed sidebar scrolls instead of clipping its contents', () => {
  // The sidebar is position:fixed, so a short viewport used to cut off the nav
  // and the profile entry with no way to reach them: scrolling the page does not
  // move a fixed element.
  const sidebar = matching('.sidebar');
  const desktop = sidebar.find(r => /position:\s*fixed/.test(r.body));
  assert.ok(desktop, 'expected a fixed .sidebar rule; found bodies: ' + JSON.stringify(sidebar.map(r => r.body.slice(0, 60))));
  assert.match(desktop.body, /overflow-y:auto/, 'a fixed sidebar must scroll when the viewport is short');
  // The mobile sidebar is static and grows with the page, so it must not scroll.
  const mobile = sheet.match(/@media\(max-width:760px\)\{[\s\S]*?\.sidebar\{([^}]*)\}/);
  assert.ok(mobile, 'expected a mobile .sidebar rule');
  assert.match(mobile[1], /overflow:visible/, 'the static mobile sidebar should not scroll');
});

test('the profile entry is a real button with the same semantics as a nav item', () => {
  assert.match(source, /<button type="button" className=\{?'user-block'/, 'user block must be a button, not a div');
  assert.match(source, /aria-current=\{page==='profile'\?'page':undefined\}/, 'must mark the current page like nav items do');
});

test('record payment is scoped to the pages that accept payments', () => {
  const match = source.match(/const takesPayments:Page\[\]=(\[[^\]]*\])/);
  assert.ok(match, 'takesPayments list must exist');
  const pages = JSON.parse(match[1].replace(/'/g, '"'));
  assert.deepEqual([...pages].sort(), ['members', 'overview', 'payments']);
});

test('the record payment button is gated on the page, not the role alone', () => {
  assert.match(
    source,
    /\{write&&takesPayments\.includes\(page\)&&<button/,
    'gating on write alone leaks the button onto reports, manage, audit and profile',
  );
});

test('no caller opens the payment form from a page that cannot take payments', () => {
  // Two call sites are legitimate: the page-gated heading button, and the
  // contextual button inside the overview guide, which sits in a block already
  // gated on page==='overview'. Any other caller would leak the form.
  // work.tsx is written as long single-line JSX, so the enclosing gate and the
  // call site share a line. Read from the start of that line, not a fixed window.
  const callers = [...source.matchAll(/setPaymentOpen\(true\)/g)].map(m =>
    source.slice(source.lastIndexOf('\n', m.index) + 1, m.index));
  assert.equal(callers.length, 2);
  callers.forEach(caller => {
    const gated = caller.includes('takesPayments.includes(page)') || caller.includes("page==='overview'");
    assert.ok(gated, 'payment form opened from a page that cannot accept payments');
  });
});
