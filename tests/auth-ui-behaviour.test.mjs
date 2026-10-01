/*
 * Behaviour tests for backend/static/auth-ui.js.
 *
 * auth-ui.js runs on the sign-in, sign-up, password change and password reset
 * pages, which are all server-rendered Django templates, so there is no test
 * harness that would execute it in this repository. These tests load the real
 * file and run it against a small DOM built from the markup Django actually
 * emits (verified against rendered responses: a <p> holding the label, the input
 * and, for password fields, a span.helptext containing the validators' <ul>).
 *
 * That makes these a statement about behaviour: which rule reaches which state,
 * when the confirmation speaks, and what submitting does. They are not a
 * statement about appearance, which is the stylesheet's job and still only
 * verifiable by a person.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../backend/static/auth-ui.js', import.meta.url), 'utf8');

// Django's four validator messages, in the order AUTH_PASSWORD_VALIDATORS lists
// them. tests/auth-ui.test.mjs checks that auth-ui.js recognises each one.
const RULES = [
  'Your password can’t be too similar to your other personal information.',
  'Your password must contain at least 6 characters.',
  'Your password can’t be a commonly used password.',
  'Your password can’t be entirely numeric.',
];

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.children = [];
    this.parentNode = null;
    // dataset and data-* attributes are one thing in a browser; auth-ui.js
    // writes dataset.statusFor and then finds it again with a [data-...] selector.
    const attribute = key => 'data-' + String(key).replace(/[A-Z]/g, letter => '-' + letter.toLowerCase());
    this.dataset = new Proxy({}, {
      get: (_target, key) => this.attributes[attribute(key)],
      set: (_target, key, value) => { this.attributes[attribute(key)] = String(value); return true; },
    });
    this.value = '';
    this.disabled = false;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this._text = '';
    this._listeners = {};
    const set = new Set();
    this.classList = {
      add: name => set.add(name),
      remove: name => set.delete(name),
      contains: name => set.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !set.has(name) : !!force;
        if (on) set.add(name); else set.delete(name);
        return on;
      },
    };
    this._classes = set;
    Object.defineProperty(this, 'className', {
      get: () => [...set].join(' '),
      set: value => { set.clear(); String(value).split(/\s+/).filter(Boolean).forEach(n => set.add(n)); },
    });
  }

  get lastChild() { return this.children[this.children.length - 1] || null; }

  get textContent() {
    return this._text + this.children.map(child => child.textContent).join('');
  }

  set textContent(value) { this._text = String(value); this.children = []; }

  setAttribute(name, value) { this.attributes[name] = String(value); }

  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }

  // The real DOM detaches a node from wherever it was before re-parenting it.
  // auth-ui.js wraps each password input, which relies on that, otherwise the
  // input would still be reachable through its old parent and be seen twice.
  detach(node) {
    if (node.parentNode && node.parentNode !== this) node.parentNode.removeChild(node);
  }

  appendChild(node) { this.detach(node); node.parentNode = this; this.children.push(node); return node; }

  insertBefore(node, ref) {
    this.detach(node);
    node.parentNode = this;
    const at = this.children.indexOf(ref);
    this.children.splice(at === -1 ? this.children.length : at, 0, node);
    return node;
  }

  focus() { this.focused = true; }

  removeChild(node) {
    const at = this.children.indexOf(node);
    if (at !== -1) {
      this.children.splice(at, 1);
      node.parentNode = null;
    }
    return node;
  }

  insertAdjacentElement(position, node) {
    assert.equal(position, 'afterend', 'only afterend is used');
    this.parentNode.insertBefore(node, this.nextSibling());
    return node;
  }

  nextSibling() {
    if (!this.parentNode) return null;
    const kids = this.parentNode.children;
    return kids[kids.indexOf(this) + 1] || null;
  }

  descendants() {
    const out = [];
    this.children.forEach(child => { out.push(child); out.push(...child.descendants()); });
    return out;
  }

  matches(selector) {
    const attr = selector.match(/^(\w+)?\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]$/);
    if (attr) {
      const [, tag, name, value] = attr;
      const tagMatches = !tag || this.tagName === tag.toUpperCase();
      return tagMatches && (value === undefined || this.getAttribute(name) === value);
    }
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    return this.tagName === selector.toUpperCase();
  }

  querySelector(selector) { return this.descendants().find(node => node.matches(selector)) || null; }

  querySelectorAll(selector) { return this.descendants().filter(node => node.matches(selector)); }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  addEventListener(type, handler) { (this._listeners[type] ||= []).push(handler); }

  fire(type, event = {}) {
    (this._listeners[type] || []).forEach(handler => handler({ preventDefault() {}, ...event }));
  }
}

function document$() {
  return {
    body: null,
    createElement: tag => new El(tag),
    createElementNS: (_ns, tag) => new El(tag),
    querySelector(selector) { return this.body.querySelector(selector); },
    querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
  };
}

function field(tag, name, attrs = {}) {
  const el = new El(tag);
  Object.keys(attrs).forEach(key => el.setAttribute(key, attrs[key]));
  el.name = name;
  return el;
}

/** The markup {{ form.as_p }} emits for the sign-up form. */
function signupDocument() {
  const doc = document$();
  const body = doc.createElement('body');
  const form = doc.createElement('form');
  form.setAttribute('method', 'post');
  form.elements = {};

  const attach = (p, input) => {
    p.appendChild(input);
    form.elements[input.name] = input;
    form.appendChild(p);
  };

  attach(doc.createElement('p'), field('input', 'username'));
  attach(doc.createElement('p'), field('input', 'email'));

  const password = doc.createElement('p');
  const p1 = field('input', 'password1', { type: 'password' });
  p1.setAttribute('aria-describedby', 'id_password1_helptext');
  form.elements.password1 = p1;
  password.appendChild(p1);
  const help = doc.createElement('span');
  help.className = 'helptext';
  help.setAttribute('id', 'id_password1_helptext');
  const list = doc.createElement('ul');
  RULES.forEach(text => {
    const li = doc.createElement('li');
    li.textContent = text;
    list.appendChild(li);
  });
  help.appendChild(list);
  password.appendChild(help);
  form.appendChild(password);

  attach(doc.createElement('p'), field('input', 'password2', { type: 'password' }));

  const submit = doc.createElement('button');
  submit.setAttribute('type', 'submit');
  submit.dataset.busy = 'Creating account…';
  submit.textContent = 'Register as a Secretary';
  form.appendChild(submit);
  body.appendChild(form);

  doc.body = body;
  return { doc, form, list, submit };
}

/** The markup {{ form.as_p }} emits for password change / reset confirm. */
function accountFormDocument() {
  const doc = document$();
  const form = doc.createElement('form');
  form.elements = {};
  const body = doc.createElement('body');
  body.appendChild(form);
  doc.body = body;

  const oldBlock = doc.createElement('p');
  const old = field('input', 'old_password', { type: 'password' });
  oldBlock.appendChild(old);
  form.appendChild(oldBlock);
  form.elements.old_password = old;

  const block = doc.createElement('p');
  const p1 = field('input', 'new_password1', { type: 'password' });
  block.appendChild(p1);
  const help = doc.createElement('span');
  help.className = 'helptext';
  const list = doc.createElement('ul');
  RULES.forEach(text => {
    const li = doc.createElement('li');
    li.textContent = text;
    list.appendChild(li);
  });
  help.appendChild(list);
  block.appendChild(help);
  form.appendChild(block);
  form.elements.new_password1 = p1;

  const confirmBlock = doc.createElement('p');
  const p2 = field('input', 'new_password2', { type: 'password' });
  confirmBlock.appendChild(p2);
  form.appendChild(confirmBlock);
  form.elements.new_password2 = p2;

  const submit = doc.createElement('button');
  submit.setAttribute('type', 'submit');
  submit.dataset.busy = 'Working…';
  form.appendChild(submit);

  return { doc, form, list, submit, old, primary: p1, confirm: p2 };
}

function load(doc) {
  vm.runInNewContext(source, { document: doc });
}

function type(input, value) {
  input.value = value;
  input.fire('input');
}

function leave(input) {
  input.fire('blur');
}

function statusOf(form, name) {
  return form.querySelector(`[data-status-for="${name}"]`);
}

test('every password box is revealed by a control that cannot submit the form', () => {
  const { doc, form } = signupDocument();
  load(doc);
  const passwords = form.querySelectorAll('input[type=password]');
  assert.equal(passwords.length, 2);
  passwords.forEach(input => {
    const toggle = input.parentNode.querySelector('button');
    assert.ok(toggle, 'a password box must gain a control');
    assert.equal(toggle.textContent, 'Show', 'a password starts hidden');
    assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  });
});

test('revealing a password keeps the value and restores the caret', () => {
  const { doc, form } = signupDocument();
  load(doc);
  const password = form.elements.password1;
  type(password, 'correct horse');
  password.selectionStart = 0;
  password.selectionEnd = 13;
  let range = null;
  password.setSelectionRange = (start, end) => { range = [start, end]; };
  const toggle = password.parentNode.querySelector('button');
  toggle.fire('click');
  assert.equal(password.type, 'text', 'the box is revealed');
  assert.equal(password.value, 'correct horse', 'the value survives the reveal');
  assert.deepEqual(range, [0, 13], 'the caret is put back where it was');
  toggle.fire('click');
  assert.equal(password.type, 'password', 'and it hides again');
  assert.equal(password.value, 'correct horse');
});

test('the checklist starts unmet and follows the rules the server applies', () => {
  const { doc, form, list } = signupDocument();
  load(doc);
  const [similar, length, common, numeric] = list.children;

  assert.equal(similar.dataset.state, 'info', 'similarity is the server\'s call');
  assert.equal(common.dataset.state, 'info', 'common-password rejection is the server\'s call');
  assert.match(similar.textContent, /Checked when you submit/);
  assert.match(common.textContent, /Checked when you submit/);
  assert.equal(length.dataset.state, 'unmet');
  assert.equal(numeric.dataset.state, 'unmet');

  type(form.elements.password1, 'short');
  assert.equal(length.dataset.state, 'unmet', 'five characters is not six');
  assert.equal(numeric.dataset.state, 'met', 'a word is not only numbers');
  assert.ok(!form.elements.password1.classList.contains('is-valid'));

  type(form.elements.password1, 'SixChars');
  assert.equal(length.dataset.state, 'met');
  assert.ok(form.elements.password1.classList.contains('is-valid'),
    'both judgeable rules pass, so the field is valid');

  type(form.elements.password1, '1234567');
  assert.equal(length.dataset.state, 'met');
  assert.equal(numeric.dataset.state, 'unmet', 'seven digits is entirely numeric');
  assert.ok(!form.elements.password1.classList.contains('is-valid'));
  assert.ok(!form.elements.password1.classList.contains('is-invalid'),
    'still typing, so the field is not shouted at');
});

test('a password only turns invalid once the field has been left', () => {
  const { doc, form } = signupDocument();
  load(doc);
  const password = form.elements.password1;
  type(password, 'abc');
  assert.ok(!password.classList.contains('is-invalid'), 'typing is not an error state');
  leave(password);
  assert.ok(password.classList.contains('is-invalid'), 'leaving it with three characters is');
  type(password, 'abcdef');
  assert.ok(!password.classList.contains('is-invalid'), 'and it recovers while fixing it');
});

test('the confirmation says nothing while empty, then answers once it can', () => {
  const { doc, form } = signupDocument();
  load(doc);
  const confirm = form.elements.password2;
  assert.equal(statusOf(form, 'password2'), null, 'no error about an untouched empty box');
  assert.ok(!confirm.classList.contains('is-invalid'));

  type(form.elements.password1, 'SixChars');
  type(confirm, 'SixChar');
  assert.equal(statusOf(form, 'password2').textContent, 'The passwords do not match.');
  assert.ok(confirm.classList.contains('is-invalid'));

  type(confirm, 'SixChars');
  assert.equal(statusOf(form, 'password2').textContent, 'The passwords match.');
  assert.ok(confirm.classList.contains('is-valid'));
  assert.ok(!confirm.classList.contains('is-invalid'));
});

test('editing the first password re-decides the confirmation', () => {
  const { doc, form } = signupDocument();
  load(doc);
  type(form.elements.password1, 'SixChars');
  type(form.elements.password2, 'SixChars');
  assert.match(statusOf(form, 'password2').textContent, /match\.$/);
  type(form.elements.password1, 'SevenChars');
  assert.equal(statusOf(form, 'password2').textContent, 'The passwords do not match.');
});

test('username and email explain themselves, and stay quiet until visited', () => {
  const { doc, form } = signupDocument();
  load(doc);
  const username = form.elements.username;
  const email = form.elements.email;

  assert.equal(statusOf(form, 'username'), null, 'a blank form has nothing to say');

  type(username, 'daniel_nortey');
  assert.match(statusOf(form, 'username').textContent, /Format looks good/);
  assert.match(statusOf(form, 'username').textContent, /checked when you submit/,
    'a valid format is not a claim that the name is free');

  type(username, 'da niel');
  assert.match(statusOf(form, 'username').textContent, /letters, numbers/);
  assert.ok(username.classList.contains('is-invalid'));

  type(email, 'daniel@');
  assert.match(statusOf(form, 'email').textContent, /valid email address/);
  type(email, 'daniel@example.com');
  assert.match(statusOf(form, 'email').textContent, /looks valid/);

  leave(username);
  assert.equal(username.getAttribute('aria-describedby'), 'status-username',
    'the hint is announced as part of the field');
});

test('sign-in is not told what a username looks like', () => {
  const { doc, form } = signupDocument();
  delete form.elements.password1;
  form.elements.password2.parentNode.removeChild(form.elements.password2);
  load(doc);
  assert.equal(statusOf(form, 'username'), null,
    'explaining the username rules on sign-in would leak them and add nothing');
});

test('password change gets the same rules and the same confirmation', () => {
  const { doc, form, list, primary, confirm } = accountFormDocument();
  load(doc);
  assert.equal(list.dataset.passwordRules, 'ready', 'the reset/change form is enhanced too');
  assert.equal(list.children[1].dataset.state, 'unmet');

  type(primary, 'SevenChars');
  assert.equal(list.children[1].dataset.state, 'met');
  assert.equal(list.children[3].dataset.state, 'met');
  assert.equal(list.children[0].dataset.state, 'info', 'still the server\'s call');

  type(confirm, 'SevenChars');
  assert.equal(statusOf(form, 'new_password2').textContent, 'The passwords match.');
  type(confirm, 'Different');
  assert.equal(statusOf(form, 'new_password2').textContent, 'The passwords do not match.');
});

test('the current-password box on password change is only given a toggle', () => {
  const { doc, form, old } = accountFormDocument();
  load(doc);
  assert.ok(old.parentNode.querySelector('button'), 'the old password can be revealed');
  assert.equal(statusOf(form, 'old_password'), null,
    'there is no rule to state about a password the server compares for you');
});

test('submitting names the state, latches once, and still lets the form send', () => {
  const { doc, form, submit } = signupDocument();
  load(doc);
  const username = form.elements.username;
  assert.ok(username.value === '', 'untouched');
  let prevented = 0;
  form.fire('submit', { preventDefault: () => { prevented += 1; } });
  assert.equal(prevented, 0, 'the browser is left free to post');
  assert.equal(submit.disabled, true, 'the button reports the request in progress');
  assert.equal(submit.textContent, 'Creating account…');

  form.fire('submit');
  assert.equal(submit.textContent, 'Creating account…', 'a second press cannot undo the latch');

  assert.ok(!form.elements.password1.disabled, 'the fields stay readable while it posts');
});