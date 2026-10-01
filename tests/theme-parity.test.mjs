import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const at = path => fileURLToPath(new URL(`../${path}`, import.meta.url));

// The browser copy is a classic script that publishes a global. Evaluating it
// here is the same way the sign-up page and the Settings form load it, so the
// test exercises the shipped file rather than a re-implementation of it.
const source = read('backend/static/theme.js');
const context = {};
new Function('window', 'globalThis', source)(context, context);
const theme = context.DuesdeskTheme;

test('the browser theme module publishes what both surfaces need', () => {
  for (const name of ['luminance', 'contrast', 'textColor', 'paletteWarnings', 'applyTheme']) {
    assert.equal(typeof theme[name], 'function', `theme.${name} must be exported`);
  }
  assert.deepEqual(theme.FIELDS, ['primary', 'secondary', 'accent']);
});

// A spread that includes the shipped default palette, both extremes, the
// greys, a near-invisible pairing and a pair of very close hues.
const PALETTES = [
  { primary: '#214f43', secondary: '#edf4e6', accent: '#527735' },
  { primary: '#000000', secondary: '#ffffff', accent: '#ff0000' },
  { primary: '#ffffff', secondary: '#000000', accent: '#ffff00' },
  { primary: '#123456', secondary: '#654321', accent: '#ffaa00' },
  { primary: '#808080', secondary: '#7f7f7f', accent: '#818181' },
  { primary: '#fefefe', secondary: '#fffffe', accent: '#fffefd' },
  { primary: '#010101', secondary: '#020202', accent: '#030303' },
  { primary: '#2b6cb0', secondary: '#1a365d', accent: '#d69e2e' },
  { primary: '#e8e8e8', secondary: '#f0f0f0', accent: '#e0e0e0' },
  { primary: '#7f1d1d', secondary: '#fef2f2', accent: '#b91c1c' },
];

// Runs branding.py in the project interpreter and returns one JSON blob, so the
// comparison is against the code that actually serves the palette.
function python() {
  const script = [
    'import json,sys',
    'sys.path.insert(0,"backend")',
    'from ledger.branding import contrast, text_color, palette_warnings',
    `palettes=json.loads(sys.argv[1])`,
    'out=[]',
    'for p in palettes:',
    '    out.append({',
    '        "colors": {k: text_color(v) for k, v in p.items()},',
    '        "luminance": {k: round(contrast(v, "#ffffff"), 10) for k, v in p.items()},',
    '        "warnings": palette_warnings(p),',
    '    })',
    'print(json.dumps(out))',
  ].join('\n');
  return JSON.parse(execFileSync(at('.venv/Scripts/python.exe'), ['-c', script, JSON.stringify(PALETTES)], {
    cwd: at('.'),
    encoding: 'utf8',
  }));
}

const expected = python();

test('textColor matches branding.py for every colour in the spread', () => {
  PALETTES.forEach((palette, index) => {
    for (const [field, value] of Object.entries(palette)) {
      assert.equal(theme.textColor(value), expected[index].colors[field],
        `textColor(${value}) must agree between theme.js and branding.py`);
    }
  });
});

test('contrast agrees with branding.py to the last decimal it reports', () => {
  PALETTES.forEach((palette, index) => {
    for (const [field, value] of Object.entries(palette)) {
      // The browser reports a ratio; the server reports the same ratio as a
      // contrast against white. Comparing the two through the same function is
      // what makes a disagreement visible.
      const browser = theme.contrast(value, '#ffffff');
      const server = expected[index].luminance[field];
      assert.ok(Math.abs(browser - server) < 1e-9,
        `contrast(${value}, #ffffff): theme.js gave ${browser}, branding.py gave ${server}`);
    }
  });
});

test('paletteWarnings reports exactly the same pairings as branding.py', () => {
  PALETTES.forEach((palette, index) => {
    assert.deepEqual(theme.paletteWarnings(palette), expected[index].warnings,
      `warnings for ${JSON.stringify(palette)} must agree`);
  });
});

test('contrast is symmetric and bounded', () => {
  assert.equal(theme.contrast('#123456', '#654321'), theme.contrast('#654321', '#123456'));
  assert.equal(theme.contrast('#ffffff', '#ffffff'), 1);
  assert.equal(theme.contrast('#000000', '#ffffff'), 21);
});

test('textColor always reaches the 4.5:1 minimum it promises', () => {
  // Every one of these is a real user choice, including the ones that look bad.
  for (const background of ['#ffffff', '#000000', '#808080', '#767676', '#777777',
    '#edf4e6', '#527735', '#214f43', '#ffff00', '#f0f8ff', '#123456']) {
    const ratio = theme.contrast(theme.textColor(background), background);
    assert.ok(ratio >= 4.5, `${background} with ${theme.textColor(background)} is only ${ratio}:1`);
  }
});

test('the palette the product ships with produces no warnings at all', () => {
  // A preview that nags about its own default teaches people to ignore it. This
  // is the invariant that keeps the warning set honest: every pairing the
  // default has to survive is one a user can actually break.
  assert.deepEqual(theme.paletteWarnings({ primary: '#214f43', secondary: '#edf4e6', accent: '#527735' }), []);
});

test('the worst-case grey pair that can fail is reported, and a safe one is not', () => {
  // #767676 on white is 4.54:1, just inside the text minimum. Paired with an
  // accent far enough away to keep the other two checks quiet.
  assert.deepEqual(theme.paletteWarnings({ primary: '#767676', secondary: '#edf4e6', accent: '#527735' }), []);
  // #777777 on white is 4.48:1, just outside it.
  const warned = theme.paletteWarnings({ primary: '#777777', secondary: '#edf4e6', accent: '#527735' });
  assert.equal(warned.length, 1);
  assert.match(warned[0].message, /primary colour/);
  assert.equal(warned[0].minimum, 4.5);
});

test('an active nav item that vanishes into the sidebar is reported', () => {
  // A dark secondary against a dark primary: the selected item disappears.
  const warnings = theme.paletteWarnings({ primary: '#214f43', secondary: '#1a2b25', accent: '#527735' });
  const nav = warnings.find(w => /selected navigation/.test(w.message));
  assert.ok(nav, 'the active nav must be reported when it disappears into the sidebar');
  assert.equal(nav.minimum, 3);
  assert.ok(nav.ratio < 3);
});

test('accent is not held to a boundary against primary, because a filled mark reads by shape', () => {
  // The two collide completely here. Nothing warns, because nothing depends on
  // telling them apart: they only meet at the logo mark.
  assert.deepEqual(theme.paletteWarnings({ primary: '#214f43', secondary: '#edf4e6', accent: '#224e42' }), []);
});

test('nearly identical colours are flagged as a boundary, not a text failure', () => {
  const warnings = theme.paletteWarnings({ primary: '#2b6cb0', secondary: '#2c6db1', accent: '#2a6caf' });
  assert.ok(warnings.length >= 1, 'the selected nav and the accent highlight are indistinguishable');
  assert.ok(warnings.some(w => w.minimum === 3));
  assert.ok(warnings.every(w => w.minimum === 4.5 || w.minimum === 3));
});

test('applyTheme writes the six variables the stylesheets read', () => {
  const written = {};
  const target = { style: { setProperty: (name, value) => { written[name] = value; } } };
  theme.applyTheme(target, { primary: '#111111', secondary: '#eeeeee', accent: '#333333' });
  assert.deepEqual(Object.keys(written).sort(), [
    '--org-accent', '--org-accent-text', '--org-primary', '--org-primary-text',
    '--org-secondary', '--org-secondary-text',
  ]);
  assert.equal(written['--org-primary'], '#111111');
  assert.equal(written['--org-secondary-text'], theme.textColor('#eeeeee'));
});

test('applyTheme can drive a scoped element instead of the document', () => {
  // The preview owns its copy so it never repaints the sign-up form behind it.
  const source2 = read('backend/static/theme.js');
  const doc = { style: { setProperty() { throw new Error('must not touch the document'); } } };
  const preview = { style: { setProperty() {} } };
  new Function('window', 'globalThis', source2)({ document: doc }, {});
  const theme2 = context.DuesdeskTheme;
  assert.doesNotThrow(() => theme2.applyTheme(preview, { primary: '#111111', secondary: '#222222', accent: '#333333' }));
});
