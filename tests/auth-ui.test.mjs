import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const authUi = read('backend/static/auth-ui.js');
const brandingJs = read('backend/static/signup-branding.js');
const themeJs = read('backend/static/theme.js');
const reactPage = read('backend/templates/react.html');
const workTsx = read('work.tsx');
const css = read('backend/static/app.css');
const settings = read('backend/config/settings.py');
const loginPage = read('backend/templates/registration/login.html');
const signupPage = read('backend/templates/registration/signup.html');
// The first-run flow has two server-rendered steps: the account form, then the
// branding step where the theme preview lives.
const brandingPage = read('backend/templates/registration/branding.html');
// One template serves change-password, the reset request and the reset link.
const accountForm = read('backend/templates/registration/account_form.html');

test('every server-rendered auth form loads the auth UI enhancement', () => {
  for (const page of ['login.html', 'signup.html', 'account_form.html']) {
    const source = { 'login.html': loginPage, 'signup.html': signupPage, 'account_form.html': accountForm }[page];
    assert.match(source, /auth-ui\.js/, `${page} must load auth-ui.js`);
  }
});

test('passwords start hidden and the toggle cannot submit the form', () => {
  assert.match(authUi, /input\[type=password\]/, 'password inputs must be enhanced');
  assert.match(authUi, /password-toggle/, 'the toggle control must have a class to style');
  // A <button> inside a <form> defaults to type=submit: without this the toggle
  // would send the form instead of revealing the password.
  assert.match(authUi, /button\.type\s*=\s*'button'/);
  assert.match(authUi, /input\.type\s*=\s*shown\s*\?\s*'password'\s*:\s*'text'/,
    'the toggle must actually swap the input type');
  assert.match(authUi, /button\.textContent = 'Show'/, 'a password must never start revealed');
  assert.match(authUi, /setAttribute\('aria-pressed', 'false'\)/,
    'the toggle must report its state to assistive technology');
});

test('revealing a password keeps the value and the caret where they were', () => {
  assert.ok(!/input\.value\s*=/.test(authUi), 'the toggle must never rewrite the field value');
  assert.match(authUi, /setSelectionRange/, 'the caret must be restored after the type change');
});

test('the checklist uses the password length the server actually enforces', () => {
  const configured = settings.match(/'min_length':\s*(\d+)/);
  const used = authUi.match(/MIN_PASSWORD\s*=\s*(\d+)/);
  assert.ok(configured, 'settings must configure a MinimumLengthValidator');
  assert.ok(used, 'auth-ui.js must carry the same minimum');
  assert.equal(used[1], configured[1],
    'the frontend minimum length must match AUTH_PASSWORD_VALIDATORS');
});

test('the checklist does not invent rules the server does not apply', () => {
  // Every configured validator's help text is matched by wording, so the list
  // describes the rule the server applies rather than a position in a list.
  for (const word of ['similar', 'character', 'common', 'numeric']) {
    assert.match(authUi, new RegExp(`'${word}'`), `the ${word} rule must be recognised`);
  }
  assert.match(authUi, /dataset\.state = 'info'/,
    'rules the browser cannot judge must say the server checks them');
  // Mixed case, symbols and character-class rules are not configured here, so
  // evaluate() must judge length and digits and nothing else.
  const start = authUi.indexOf('function evaluate(');
  const body = authUi.slice(start, authUi.indexOf('}', start));
  assert.ok(!/case|symbol|character|upper|lower/i.test(body),
    'no rule the server does not apply may be judged in the browser');
});

test('only the rules a browser can judge are marked met or unmet', () => {
  assert.match(authUi, /evaluate\(kind, value\)/);
  assert.match(authUi, /value\.length >= MIN_PASSWORD/);
  assert.match(authUi, /!\/\^\\d\+\$\/\.test\(value\)/, 'the numeric rule must match the validator');
});

test('username, email and confirmation are checked on the sign-up form', () => {
  for (const name of ['username', 'email']) {
    assert.match(authUi, new RegExp(`\\b${name}\\b`), `${name} must have a rule`);
  }
  assert.match(authUi, /primaryField/, 'the confirmation must find the first password');
  assert.match(authUi, /primary\.value === confirm\.value/,
    'the confirmation must be compared against the first password');
  assert.match(authUi, /aria-live/, 'the hints must be announced, not colour-only');
  assert.match(authUi, /only the field's own help text|help text/i);
});

test('the confirmation stays quiet while it is empty', () => {
  assert.match(authUi, /if \(!input\.value && !touched\)/,
    'an untouched empty field must not be marked as an error');
});

test('submitting locks the button but never blocks the native POST', () => {
  assert.match(authUi, /addEventListener\('submit'/, 'submission must be observed');
  assert.match(authUi, /button\.dataset\.busyLocked/,
    'the state must latch, so a second submit cannot re-enable the button');
  assert.match(authUi, /button\.disabled = true/);
  const start = authUi.indexOf("addEventListener('submit'");
  const handler = authUi.slice(start, authUi.indexOf('});', start));
  assert.ok(!handler.includes('preventDefault'),
    'the browser must still be free to send the form it was going to send');
  assert.ok(!/form\.elements\[[^\]]+\]\.disabled/.test(authUi),
    'only the button may be disabled; the fields must stay inspectable');
});

test('each auth submit button carries a busy label', () => {
  for (const [name, page] of [['sign in', loginPage], ['sign up', signupPage], ['account form', accountForm]]) {
    assert.match(page, /type="submit" data-busy="/, `${name} must name its in-flight state`);
  }
});

test('the branding page loads the stylesheets the preview needs, in order', () => {
  // The preview moved to the first-run branding step, which is the only
  // server-rendered page that paints a workspace. It is drawn by the application
  // stylesheet, which declares bare elements and shared class names, so the
  // isolation sheet has to load after it. A literal name is used rather than a
  // context variable because verify_static reads these tags to decide what must
  // be collected.
  const sheets = [...brandingPage.matchAll(/<link rel="stylesheet" href="([^"]+)"/g)].map(m => m[1]);
  assert.ok(sheets.includes("{% static 'app/style.css' %}"),
    'the application stylesheet must be loaded');
  assert.ok(sheets.includes("{% static 'theme-preview.css' %}"),
    'the isolation stylesheet must be loaded');
  assert.ok(sheets.indexOf("{% static 'app/style.css' %}") < sheets.indexOf("{% static 'theme-preview.css' %}"),
    'the isolation must load after the application sheet, or it cannot undo it');
});

test('the sign-up submit button still names its in-flight state', () => {
  assert.match(signupPage, /type="submit" data-busy="/,
    'a rewritten page must keep the busy label auth-ui.js depends on');
});

test('the application shell loads theme.js before its own bundle', () => {
  // work.tsx reads the contrast maths off window.DuesdeskTheme. A module bundle is
  // deferred and a classic script is not, so placing theme.js first is what
  // guarantees it exists by the time the bundle runs.
  const classic = reactPage.indexOf("<script src=\"{% static 'theme.js' %}\"");
  const module = reactPage.indexOf('<script type="module"');
  assert.ok(classic > -1, 'react.html must load theme.js');
  assert.ok(module > -1, 'react.html must load the application bundle');
  assert.ok(classic < module, 'theme.js must come before the bundle');
});

test('the React Settings preview no longer computes contrast itself', () => {
  // One definition of readable: branding.py for the server, theme.js for the
  // browser. A second implementation in work.tsx is the drift the parity test
  // exists to prevent.
  assert.doesNotMatch(workTsx, /function contrastText/,
    'work.tsx must not carry its own contrast implementation');
  assert.doesNotMatch(workTsx, /0\.2126|0\.7152|0\.0722/,
    'work.tsx must not restate the luminance coefficients');
  assert.match(workTsx, /from '\.\/theme-bridge'/,
    'work.tsx must read contrast through the bridge');
});

test('the branding theme preview themes the whole page, not only the box', () => {
      // The variables are now written by theme.applyTheme rather than inline, so
      // that the browser and the server cannot disagree about what makes a
      // colour readable. What matters here is that the preview still drives the
      // page as a whole, and that the only thing doing the writing is the module
      // both sides share.
      assert.match(brandingJs, /applyTheme\(document\.documentElement/,
        'the preview must set variables on the root element');
      assert.doesNotMatch(brandingJs, /setProperty\(/,
        'signup-branding.js must not write --org-* variables itself');
      assert.doesNotMatch(brandingJs, /luminance|0\.2126|0\.7152/,
        'signup-branding.js must not restate the contrast maths');
      assert.match(themeJs, /setProperty\(`--org-/,
        'theme.js must be what writes the --org-* variables');
      assert.match(themeJs, /applyTheme/,
        'theme.js must export the function signup-branding.js calls');
      // theme.js is a classic script and signup-branding.js defers, so placing
      // theme.js first is what guarantees window.DuesdeskTheme exists.
      const classic = brandingPage.indexOf("<script src=\"{% static 'theme.js' %}\"");
      const enhancer = brandingPage.indexOf("{% static 'signup-branding.js' %}");
      assert.ok(classic > -1, 'branding.html must load theme.js');
      assert.ok(enhancer > -1, 'branding.html must load signup-branding.js');
      assert.ok(classic < enhancer, 'theme.js must load before signup-branding.js');
    });

test('the stylesheet styles the toggle, the rule list and the hidden state', () => {
  assert.match(css, /\.password-toggle\b/);
  assert.match(css, /\.password-toggle:focus-visible/);
  assert.match(css, /\.field-status\b/);
  assert.match(css, /input\.is-invalid\b/, 'invalid fields must be visibly marked');
  assert.match(css, /\.password-rules\b/, 'the rule list needs its own layout');
  assert.match(css, /\.rule-mark\b/, 'each rule needs a marker that is not colour');
  assert.match(css, /\.sr-only\b/, 'state words must be available to screen readers');
  assert.match(css, /\.button:disabled\b/, 'a submitting button must look submitting');
});
