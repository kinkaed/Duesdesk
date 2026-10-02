import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../work.tsx', import.meta.url), 'utf8');
const css = fs.readFileSync(new URL('../styles.css', import.meta.url), 'utf8');
const api = fs.readFileSync(new URL('../api.ts', import.meta.url), 'utf8');

const sheet = css.replace(/\/\*[\s\S]*?\*\//g, '');
const rules = () => [...sheet.matchAll(/(^|[\n};])\s*([^{}\n][^{}]*?)\s*\{([^}]*)\}/g)]
  .map(m => ({ selectors: m[2].split(',').map(s => s.trim()), body: m[3].trim() }));
const matching = selector => rules().filter(r => r.selectors.includes(selector));
// The audit component, from its type declaration to the end of the module section.
const audit = source.slice(source.indexOf('type AuditFilters'), source.indexOf('function AuditLog'));
const list = source.slice(source.indexOf('function AuditLog'));

// A boundary that does not exist slices from -1 and yields a string that is
// almost right, so a missing marker fails here instead of as a mystery diff.
const section = (from, to) => {
  const start = source.indexOf(from);
  const end = source.indexOf(to);
  assert.notEqual(start, -1, `no marker ${from} in work.tsx`);
  assert.ok(end > start, `marker ${to} does not follow ${from}`);
  return source.slice(start, end);
};

test('the list is not handed the data the detail view exists to reveal', () => {
  // The whole point of the split is that 100 rows of JSON never cross the wire.
  // Asserted on the backend contract, and here on the fact that the row type the
  // list renders has no details field for a row to accidentally read.
  const rowType = api.slice(api.indexOf('export type AuditEvent='), api.indexOf('export type AuditChange='));
  assert.doesNotMatch(rowType, /\bdetails\b/, 'AuditEvent still carries details, so the list can render it');
  assert.match(api.slice(api.indexOf('export type AuditDetail=')),
    /details:Record<string,unknown>/, 'the detail response must still carry the full record');
  // And the detail is fetched per row, on open, rather than with the page.
  assert.match(audit, /api<AuditDetail>\('\/api\/audit\/'\+event\.id\+'\/'\)/);
  assert.doesNotMatch(audit, /\/api\/audit\/'\+auditQuery/, 'the detail request must not carry the list filters');
});

test('the opened row speaks plain language, not machine values', () => {
  // The reader is an office administrator, not a developer. A dotted action
  // code, a UUID request reference, an endpoint path, a reason slug or a JSON
  // blob is jargon, and every one of them used to be on the page.
  const detail = section('className="audit-detail"', 'function AuditLog');
  ['JSON.stringify', 'audit-technical', '<code>', 'detail.action', 'detail.details',
   'detail.http_method', 'detail.path', '<dt>Raw reason</dt>', '<dt>Endpoint</dt>',
   '<dt>Reference</dt>', '<dt>Event</dt>'].forEach(value => {
    assert.ok(!detail.includes(value), `the opened row still shows ${value}`);
  });
  // And no row anywhere still renders the raw action name as a subscript.
  assert.ok(!audit.includes('{event.action}'), 'the summary still prints the action code');
  // What a reader does need is present, in words: who, when, what changed, and
  // where the change came from -- the last being why this is an audit record.
  assert.match(detail, /<dt>Account<\/dt>/);
  assert.match(detail, /<dt>Recorded<\/dt>/);
  assert.match(detail, /<AuditChanges changes=\{detail\.changes\}\/>/);
  assert.match(detail, /<dt>\{detail\.ip_is_peer\?'Connection address':'IP address'\}<\/dt>/);
  assert.match(detail, /<dd>\{detail\.ip_address\|\|'Not recorded'\}{detail\.ip_is_peer&&' · the reverse proxy, not the member'}<\/dd>/);
  assert.match(detail, /<dt>Browser<\/dt><dd>\{detail\.user_agent\|\|'Not recorded'\}<\/dd>/);
});

test('an unknown reason cannot leak a Python exception class name', () => {
  // import.failed records the raised exception's class name. Showing it in the
  // summary would be both unhelpful and a version-dependent leak, so the label
  // has to come from a fixed map with a generic fallback.
  const taxonomy = fs.readFileSync(new URL('../backend/ledger/audit_taxonomy.py', import.meta.url), 'utf8');
  const reasonLabels = taxonomy.slice(taxonomy.indexOf('REASON_LABELS = {'), taxonomy.indexOf('\n}', taxonomy.indexOf('REASON_LABELS = {')));
  assert.doesNotMatch(reasonLabels, /Error|Exception|except|Traceback/,
    'a reason label must never be an exception class name');
  assert.match(taxonomy, /def reason_label[\s\S]*?UNKNOWN_REASON_LABEL/,
    'an unrecognized reason must fall back rather than be echoed back');
  assert.match(api, /reason_label/);
});

test('search waits for a pause instead of firing per keystroke', () => {
  assert.match(list, /setTimeout\([\s\S]*?300\)/);
  assert.match(list, /clearTimeout\(timer\)/, 'the pending search must be cancelled, not stacked');
  // The committed value is what triggers the request, so a partial word is never
  // sent while the reader is still typing it.
  const effect = list.slice(list.indexOf('useEffect'), list.indexOf('useEffect') + 400);
  assert.ok(list.includes('useEffect'), 'the audit list must react to its filters');
  assert.match(effect, /current\.q===search\?current/);
});

test('every filter change returns the reader to the first page', () => {
  // Resetting the page inside each handler is the obvious thing to forget on one
  // control, which strands the reader on an empty page N.
  const change = section('const change=', 'const clear=');
  assert.match(change, /setPage\(1\)/);
  // And no control sets a page without going through that helper.
  const setters = [...list.matchAll(/onChange=\{e=>(\w+)\(\{/g)].map(m => m[1]);
  assert.ok(setters.length > 0, 'the audit filters should exist to be checked');
  assert.ok(setters.every(fn => fn === 'change'), 'a filter bypasses change() and skips the page reset');
});

test('the query carries no empty parameters', () => {
  const query = section('const auditQuery=', 'const TIME_UNITS:');
  // An empty value serialized as ?category= would be a filter that matches
  // nothing, and a reader who cleared a box would see an empty history.
  [...query.matchAll(/if\((f\.[a-z_]+)\)params\.set/g)].forEach(m => {
    assert.ok(!/if\(!f\./.test(m[0]), 'a param is always set, so an empty filter is sent');
  });
  // Page 1 is the default, so it must not appear in the URL.
  assert.match(query, /if\(page>1\)params\.set\('page',String\(page\)\)/);
  assert.ok(/return query\?'\?'\+query:''/.test(query),
    'no filters and no page must request the bare path, with no stray "?"');
});

test('relative time is shown but the exact time stays available', () => {
  const relative = section('const TIME_UNITS:', '// Rendered inside a sentence');
  // A unit table, so "in 3 weeks" and "in 2 months" are both right: a fixed
  // divisor would drift from the calendar as soon as a month is involved.
  assert.match(relative, /TIME_UNITS:\[string,number\]\[\]=\[/);
  assert.match(relative, /step\[1\]/);
  // Singular/plural, so a reader never sees "1 minutes ago".
  assert.match(relative, /\$\{count\} \$\{step\[0\]\}\$\{count===1\?'\':'s'\} ago/);
  // The machine-readable and hover values carry the exact moment.
  assert.match(audit, /<time className="audit-when" dateTime=\{event\.date\} title=\{exact\}>/);
  assert.match(audit, /<dt>Recorded<\/dt><dd>\{exact\}<\/dd>/);
});

test('a missing value renders as a dash rather than "undefined"', () => {
  // The record is arbitrary JSON written by many call sites, so a null or a
  // missing key is expected traffic, not an exception to be handled at each use.
  const show = section('const show=', 'const fieldName=');
  assert.match(show, /===null\|\|value===undefined\|\|value===''\?'—'/);
});

test('the four states are distinguishable and announced', () => {
  // Loading, error, no events, and no matches are four different situations and
  // a reader who conflated them would think their filters deleted the history.
  assert.match(list, /<div className="loading" role="status">Loading history…<\/div>/);
  assert.match(list, /\{error\?<Alert>\{error\}/);
  assert.match(list, /:active\.length>0\?<Empty>No events match these filters/);
  assert.match(list, /:<Empty>No audit events recorded yet\.<\/Empty>/);
  // The list is live-region friendly: the count of rows is not re-announced, but
  // the in-flight state is.
  assert.match(audit, /<p className="loading" role="status">Loading details…<\/p>/);
});

test('the detail fetch is not cancelled by its own loading state', () => {
  // The row used to depend on `busy`. Setting busy to true re-ran the effect,
  // whose cleanup invalidated the in-flight request; the re-run then returned
  // early on busy, so the response was discarded and the row read "Loading
  // details…" forever. No state the effect itself sets may be a dependency.
  const effect = section('const loaded=useRef(', 'const exact=');
  const deps = /\[([^\]]*)\]\);\s*$/.exec(effect.trim())?.[1].split(',').map(s => s.trim()) ?? [];
  assert.deepEqual(deps, ['open', 'event.id', 'retry'],
    'the effect may depend only on things changed from outside it');
  // The guard is a ref, which cannot trigger a re-run, and retry is the only way
  // back in after a failure -- a plain setDetail(null) would now do nothing.
  assert.match(effect, /if\(!open\|\|loaded\.current===event\.id\)return;/);
  assert.match(effect, /loaded\.current=event\.id;/);
  assert.match(audit, /setError\(''\);setRetry\(n=>n\+1\);/);
});

test('the row is operable by keyboard and exposes its state', () => {
  assert.match(audit, /<button type="button" className="audit-summary" aria-expanded=\{open\}/);
  assert.match(audit, /className="audit-summary" aria-expanded=\{open\} onClick=\{\(\)=>setOpen\(v=>!v\)\}/,
    'the summary must be a button that reports and toggles its open state');
  const ring = matching('.audit-summary:focus-visible');
  assert.ok(ring.length, 'the row needs a visible focus ring');
  assert.ok(ring.some(r => /(^|;)\s*outline\s*:/.test(r.body)));
  // The changes table is a table, so it is captioned and its headers are scoped.
  assert.match(audit, /<caption className="sr-only">Values before and after this change<\/caption>/);
  assert.match(audit, /<th scope="col">Before<\/th>/);
  assert.match(audit, /<th scope="row">\{fieldName\(c\.field\)\}<\/th>/);
  // Every control is named, so a screen reader reaches a filter by its purpose.
  ['Search audit history', 'Category', 'Outcome', 'Account', 'From date', 'To date'].forEach(name => {
    assert.ok(audit.includes(`aria-label="${name}"`) || list.includes(`aria-label="${name}"`),
      `the "${name}" control is unlabelled`);
  });
  // A removed filter says which filter it removes.
  assert.match(list, /aria-label=\{`Remove the \$\{chip\.kind\.toLowerCase\(\)\} filter`\}/);
});

test('every severity the API can return has a rail in the stylesheet', () => {
  // The rail is the only place severity is visible on a collapsed row, so a
  // severity with no rule is a critical event that looks routine.
  const severities = ['notice', 'warning', 'critical'];
  const rails = rules().filter(r => r.selectors.some(s => s.startsWith('.audit-rail.severity-')));
  assert.ok(rails.length, 'no severity rails are defined');
  assert.ok(severities.every(s => rails.some(r => r.selectors.includes(`.audit-rail.severity-${s}`))),
    `missing a rail for: ${severities.filter(s => !rails.some(r => r.selectors.includes(`.audit-rail.severity-${s}`))).join(', ')}`);
  // The rail carries the colour, so the row text must not depend on it alone:
  // the outcome chip is what names the outcome in words.
  assert.ok(rails.every(r => r.body.includes('background:')), 'a severity rail must be distinguishable by colour');
  assert.match(audit, /className=\{'audit-chip '\+event\.outcome\}/);
  // A non-success outcome is coloured as a problem, not styled as routine.
  // Checked across the pair, because a combined selector is split when parsed.
  const problem = rules().find(r => r.selectors.includes('.audit-chip.denied') && r.selectors.includes('.audit-chip.failure'));
  assert.ok(problem, 'a denied or failed event must not look like a routine one');
  assert.match(problem.body, /var\(--danger/);
});

test('the audit rules only use variables the theme defines', () => {
  // A typo in a custom property renders as an empty declaration and the rule
  // silently loses the property, so the reference is checked against the sheet.
  const defined = new Set([...sheet.matchAll(/(--[a-z-]+)\s*:/g)].map(m => m[1]));
  const auditRules = rules().filter(r => r.selectors.some(s => s.includes('audit')));
  assert.ok(auditRules.length > 10, 'the audit stylesheet looks suspiciously thin');
  const missing = new Set();
  auditRules.forEach(r => [...r.body.matchAll(/var\((--[a-z-]+)\)/g)].forEach(m => {
    if (!defined.has(m[1])) missing.add(m[1]);
  }));
  assert.deepEqual([...missing], [], `undefined custom properties: ${[...missing].join(', ')}`);
  // A literal colour here is one the organization branding cannot re-theme, so
  // the audit view would be the only part of the app that ignores the palette.
  const literal = auditRules.filter(r => /#[0-9a-f]{3,8}\b|rgba?\(/.test(r.body));
  assert.deepEqual(literal.map(r => `${r.selectors.join(', ')}: ${r.body}`), [],
    'audit rules must colour through the theme variables, not literals');
});

test('the severity rail is a real ramp, not three names for one colour', () => {
  const rail = severity => matching(`.audit-rail.severity-${severity}`)[0]?.body ?? '';
  const colours = ['routine', 'notice', 'warning', 'critical'].map(s => {
    const body = s === 'routine' ? matching('.audit-rail')[0]?.body ?? '' : rail(s);
    const colour = /background:\s*([^;]+)/.exec(body)?.[1]?.trim();
    assert.ok(colour, `severity "${s}" has no background colour`);
    return colour;
  });
  assert.equal(new Set(colours).size, colours.length,
    `severities share a colour: ${colours.join(' / ')}`);
  // The outcome chip carries the meaning in words, so the rail is decoration and
  // a reader who cannot see colour is not missing the outcome.
  assert.match(audit, /className=\{'audit-chip '\+event\.outcome\}>\{event\.outcome_label\}/);
});

test('the old raw-JSON audit markup is gone', () => {
  // The pre-redesign page rendered <details> per event with the details JSON as
  // the only content. Nothing may reintroduce that shape.
  assert.doesNotMatch(source, /className="audit-entry"/);
  assert.doesNotMatch(source, /\{\/\* ?Audit history[\s\S]*?JSON\.stringify\(e\.details/);
  assert.doesNotMatch(css, /\.audit-entry/);
  // and the replacement styles exist
  ['.audit-list', '.audit-summary', '.audit-detail', '.audit-changes', '.audit-chips'].forEach(selector => {
    assert.ok(matching(selector).length, `no styles for ${selector}`);
  });
});
