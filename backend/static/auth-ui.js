/*
 * Progressive enhancement for the sign-in, sign-up, password change and
 * password reset pages.
 *
 * Additive only. Every field still submits, the native POST is never
 * intercepted, and the server stays the only authority. With JavaScript off
 * these pages behave exactly as they did before, including Django's own static
 * password-rule list.
 *
 *   1. A Show / Hide control on every password box.
 *   2. Live hints under the sign-up username and email fields.
 *   3. A live state on the password-rule list Django already renders, driven by
 *      the validators that are actually configured. Only the rules the browser
 *      can know are marked met or unmet; the ones only the server can judge say
 *      so instead of pretending to pass.
 *   4. Match feedback on the confirmation box.
 *   5. One-shot submit state so a double tap cannot post twice.
 */
(() => {
  const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  // UnicodeUsernameValidator, which is what User.username actually enforces.
  const USERNAME = /^[\w.@+-]+$/;
  // Mirrors config.settings.AUTH_PASSWORD_VALIDATORS. tests/auth-ui.test.mjs
  // reads the setting and fails if the two ever drift apart.
  const MIN_PASSWORD = 6;

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(key => node.setAttribute(key, attrs[key]));
    return node;
  }

  // A marker that carries the state without relying on colour: a tick when the
  // rule is met, an open ring when it is not, a ringed dot for the rules only
  // the server can judge.
  function mark(kind) {
    const svg = svgEl('svg', { viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false' });
    svg.setAttribute('class', 'rule-mark rule-mark-' + kind);
    if (kind === 'met') {
      svg.appendChild(svgEl('path', { d: 'M3.6 8.4l2.9 2.9 5.9-6.6' }));
    } else if (kind === 'info') {
      svg.appendChild(svgEl('circle', { cx: '8', cy: '8', r: '6' }));
      svg.appendChild(svgEl('circle', { cx: '8', cy: '8', r: '1.1', 'class': 'rule-mark-dot' }));
    } else {
      svg.appendChild(svgEl('circle', { cx: '8', cy: '8', r: '4.3' }));
    }
    return svg;
  }

  // ---------------------------------------------------------------- show/hide
  function addToggle(input) {
    if (input.dataset.passwordToggle === 'ready') return;
    input.dataset.passwordToggle = 'ready';
    const wrap = document.createElement('span');
    wrap.className = 'password-field';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'password-toggle';
    button.textContent = 'Show';
    button.setAttribute('aria-label', 'Show password');
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', event => {
      // A button inside a label would otherwise forward the click to the input.
      event.preventDefault();
      const shown = input.type === 'text';
      // Revealing a password must not throw away the caret position.
      const start = input.selectionStart;
      const end = input.selectionEnd;
      input.type = shown ? 'password' : 'text';
      button.textContent = shown ? 'Show' : 'Hide';
      button.setAttribute('aria-pressed', String(!shown));
      button.setAttribute('aria-label', shown ? 'Show password' : 'Hide password');
      input.focus();
      if (start !== null && end !== null) {
        try {
          input.setSelectionRange(start, end);
        } catch (error) {
          // Some inputs refuse a selection range; the reveal still worked.
        }
      }
    });
    wrap.appendChild(button);
  }

  document.querySelectorAll('input[type=password]').forEach(addToggle);

  // ------------------------------------------------------------ field status
  function describedBy(input, id) {
    const current = input.getAttribute('aria-describedby');
    if (!current) {
      input.setAttribute('aria-describedby', id);
    } else if (current.split(/\s+/).indexOf(id) === -1) {
      input.setAttribute('aria-describedby', current + ' ' + id);
    }
  }

  // The node a status line belongs after: the password wrapper when there is
  // one, otherwise the field's own help text, so the static rules stay directly
  // under the input and the live hint reads after them.
  function anchorFor(input) {
    const wrap = input.closest('.password-field');
    if (wrap) return wrap;
    const scope = input.closest('p') || input.parentElement;
    const help = scope ? scope.querySelector('.helptext') : null;
    return help || input;
  }

  function statusNode(anchor, name) {
    const scope = anchor.parentNode;
    let node = scope.querySelector('[data-status-for="' + name + '"]');
    if (node) return node;
    node = document.createElement('p');
    node.className = 'field-status';
    node.dataset.statusFor = name;
    node.setAttribute('aria-live', 'polite');
    node.id = 'status-' + name;
    anchor.insertAdjacentElement('afterend', node);
    return node;
  }

  // Nothing is shown about a field that has not been visited and is still
  // empty: red marks on a blank form are noise, not guidance. Once the field
  // has been left, an empty value is reported as the omission it is.
  function paintStatus(input, node, message, validText) {
    const touched = input.dataset.touched === 'true';
    if (!input.value && !touched) {
      input.classList.remove('is-valid', 'is-invalid');
      node.textContent = '';
      node.classList.remove('valid', 'invalid');
      return;
    }
    input.classList.toggle('is-valid', !message);
    input.classList.toggle('is-invalid', !!message);
    node.textContent = message || validText;
    node.classList.toggle('valid', !message);
    node.classList.toggle('invalid', !!message);
  }

  // ------------------------------------------------------- username and email
  const TEXT_RULES = {
    username(value) {
      if (!value) return 'Enter a username.';
      if (value.length > 150) return 'Usernames are limited to 150 characters.';
      if (!USERNAME.test(value)) return 'Use only letters, numbers and . @ + - _.';
      return '';
    },
    email(value) {
      if (!value) return 'Enter an email address.';
      if (!EMAIL.test(value)) return 'Enter a valid email address, like name@example.com.';
      return '';
    },
  };
  const TEXT_OK = {
    username: 'Format looks good. Whether it is free is checked when you submit.',
    email: 'Email address looks valid.',
  };

  function checkText(form, name) {
    const input = form.elements[name];
    if (!input || !TEXT_RULES[name]) return;
    const node = statusNode(anchorFor(input), name);
    describedBy(input, node.id);
    paintStatus(input, node, TEXT_RULES[name](input.value), TEXT_OK[name]);
  }

  // ------------------------------------------------------- the password rules
  // Each entry of Django's help text is matched by wording rather than by
  // position, so a validator added or reordered in settings cannot make the
  // checklist describe a different rule than the one the server applies.
  function ruleKind(text) {
    const words = String(text).toLowerCase();
    if (words.indexOf('similar') !== -1) return 'similar';
    if (words.indexOf('character') !== -1) return 'length';
    if (words.indexOf('common') !== -1) return 'common';
    if (words.indexOf('numeric') !== -1) return 'numeric';
    return null;
  }

  // Only the rules a browser can actually judge. Similarity and
  // common-password rejection are server decisions and are never guessed at.
  function evaluate(kind, value) {
    if (kind === 'length') return value.length >= MIN_PASSWORD;
    if (kind === 'numeric') return value !== '' && !/^\d+$/.test(value);
    return null;
  }

  function enhanceRules(primary) {
    const scope = primary.closest('p') || primary.parentElement;
    const help = scope ? scope.querySelector('.helptext') : null;
    const list = help ? help.querySelector('ul') : null;
    if (!list || list.dataset.passwordRules === 'ready') return null;
    list.dataset.passwordRules = 'ready';
    list.classList.add('password-rules');
    const items = [];
    Array.prototype.forEach.call(list.children, li => {
      const kind = ruleKind(li.textContent);
      const checkable = kind === 'length' || kind === 'numeric';
      const state = document.createElement('span');
      state.className = 'sr-only';
      li.insertBefore(mark(checkable ? 'unmet' : 'info'), li.firstChild);
      li.appendChild(state);
      items.push({ li: li, kind: kind, checkable: checkable, state: state });
    });
    return function refresh(value) {
      let checkable = 0;
      let met = 0;
      items.forEach(item => {
        if (!item.checkable) {
          item.li.dataset.state = 'info';
          item.state.textContent = ' Checked when you submit.';
          return;
        }
        checkable += 1;
        const ok = evaluate(item.kind, value);
        item.li.dataset.state = ok ? 'met' : 'unmet';
        item.state.textContent = ok ? ' Met.' : ' Not met yet.';
        if (ok) met += 1;
      });
      return { checkable: checkable, met: met };
    };
  }

  // A rule list is guidance, not a verdict: the field is only marked valid once
  // the rules the browser can judge all pass, and only marked invalid after the
  // user leaves it, never mid-keystroke.
  function updatePassword(input, refresh) {
    const result = refresh(input.value);
    const touched = input.dataset.touched === 'true';
    const complete = input.value !== '' && result.checkable === result.met;
    input.classList.toggle('is-valid', complete);
    input.classList.toggle('is-invalid', !!input.value && !complete && touched);
  }

  // ------------------------------------------------------------- confirmation
  function primaryField(form) {
    return form.elements['password1'] || form.elements['new_password1'];
  }

  function confirmField(form) {
    return form.elements['password2'] || form.elements['new_password2'];
  }

  function checkConfirm(form) {
    const confirm = confirmField(form);
    if (!confirm) return;
    const primary = primaryField(form);
    const node = statusNode(anchorFor(confirm), confirm.name);
    describedBy(confirm, node.id);
    let message = 'Repeat your password.';
    if (confirm.value) {
      message = primary && primary.value === confirm.value ? '' : 'The passwords do not match.';
    }
    paintStatus(confirm, node, message, 'The passwords match.');
  }

  // ------------------------------------------------------------------ wiring
  function enhanceForm(form) {
    if (form.dataset.authUi === 'ready') return;
    form.dataset.authUi = 'ready';

    // The sign-up form is the only one that collects an account identity, so
    // identity hints belong to it alone; sign-in must not explain its rules.
    const isSignup = !!form.elements['password1'];
    if (isSignup) {
      ['username', 'email'].forEach(name => {
        const input = form.elements[name];
        if (!input) return;
        input.addEventListener('input', () => checkText(form, name));
        input.addEventListener('blur', () => {
          input.dataset.touched = 'true';
          checkText(form, name);
        });
      });
    }

    const primary = primaryField(form);
    if (primary) {
      const refresh = enhanceRules(primary);
      if (refresh) {
        // Editing the first password decides whether the confirmation still
        // matches, so it is re-checked while typing, not only when focus leaves.
        primary.addEventListener('input', () => {
          updatePassword(primary, refresh);
          checkConfirm(form);
        });
        primary.addEventListener('blur', () => {
          primary.dataset.touched = 'true';
          updatePassword(primary, refresh);
          checkConfirm(form);
        });
        updatePassword(primary, refresh);
      }
    }

    const confirm = confirmField(form);
    if (confirm) {
      confirm.addEventListener('input', () => checkConfirm(form));
      confirm.addEventListener('blur', () => {
        confirm.dataset.touched = 'true';
        checkConfirm(form);
      });
    }
  }

  function lockOnSubmit(form) {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (!button || button.dataset.busyLocked === 'true') return;
      button.dataset.busyLocked = 'true';
      if (button.dataset.busy) button.textContent = button.dataset.busy;
      // Only the button is disabled. The form is deliberately not prevented,
      // so the browser still sends the request it would have sent anyway.
      button.disabled = true;
    });
  }

  document.querySelectorAll('form').forEach(form => {
    enhanceForm(form);
    lockOnSubmit(form);
  });
})();
