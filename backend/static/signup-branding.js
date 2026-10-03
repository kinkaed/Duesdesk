/*
 * The branding step's progressive enhancement.
 *
 * Without JavaScript the form posts straight to /signup/branding/, which saves
 * the name, colours and logo and finishes the first run. With JavaScript the
 * page paints a live preview and, on Save, reuses the same two endpoints the
 * rest of the application uses: POST /api/settings/ to store the branding, then
 * /signup/branding/ to mark the step done. Splitting it that way means the
 * browser never has a private copy of the save logic, and the no-JS path stays a
 * legitimate way to finish.
 */
(() => {
const form = document.querySelector('#branding-form');
if (!form) return;

const theme = window.DuesdeskTheme;
const DEFAULTS = { primary: '#214f43', secondary: '#edf4e6', accent: '#527735' };
const fields = theme ? theme.FIELDS : ['primary', 'secondary', 'accent'];

const reset = document.querySelector('#theme-reset');
const changed = document.querySelector('#theme-changed');
const warnings = document.querySelector('#theme-warnings');
const hexLabels = {};
fields.forEach(k => { hexLabels[k] = document.querySelector(`[data-theme-hex="${k}"]`); });

function palette() {
  const out = {};
  fields.forEach(k => { out[k] = form.elements[k].value; });
  return out;
}

function sameAsDefault(current) {
  return fields.every(k => current[k] === DEFAULTS[k]);
}

function paint() {
  const current = palette();
  theme.applyTheme(document.documentElement, current);
  fields.forEach(k => { if (hexLabels[k]) hexLabels[k].textContent = current[k]; });
  const mark = document.querySelector('#theme-preview-mark');
  if (mark) {
    const logo = document.querySelector('#logo-preview');
    if (logo && !logo.hidden) {
      mark.style.background = 'transparent';
      mark.style.backgroundImage = `url(${logo.src})`;
      mark.style.backgroundSize = 'contain';
      mark.style.backgroundRepeat = 'no-repeat';
      mark.style.backgroundPosition = 'center';
      mark.textContent = '';
    } else {
      mark.removeAttribute('style');
      mark.textContent = 'd.';
    }
  }
  if (reset) reset.hidden = sameAsDefault(current);
  if (changed) changed.hidden = sameAsDefault(current);
  if (warnings) {
    const found = theme.paletteWarnings(current);
    warnings.innerHTML = found.length
      ? found.map(w => `<p class="theme-warning"><span aria-hidden="true">!</span><span>${w.message} <em>Contrast is ${w.ratio}:1; ${w.minimum}:1 is the minimum.</em></span></p>`).join('')
      : '';
  }
}

fields.forEach(k => {
  const input = form.elements[k];
  input.addEventListener('input', paint);
});

if (reset) {
  reset.addEventListener('click', () => {
    fields.forEach(k => {
      form.elements[k].value = DEFAULTS[k];
      form.elements[k].dispatchEvent(new Event('input', { bubbles: true }));
    });
    form.elements.primary.focus();
  });
}

paint();

const upload = document.querySelector('#logo-upload');
let version = 0;
if (upload) {
  upload.addEventListener('change', async () => {
    const current = ++version;
    form.elements.logo_token.value = '';
    document.querySelector('#logo-error').textContent = '';
    const logo = document.querySelector('#logo-preview');
    logo.hidden = true;
    const file = upload.files[0];
    if (!file) return;
    try {
      const data = new FormData();
      data.append('logo', file);
      const response = await fetch('/api/branding/preview/', {
        method: 'POST',
        headers: { 'X-CSRFToken': form.elements.csrfmiddlewaretoken.value },
        body: data,
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'Unable to read logo.');
      if (current !== version) return;
      form.elements.logo_token.value = result.logo_token;
      fields.forEach(k => { form.elements[k].value = result[k]; });
      paint();
      const reader = new FileReader();
      reader.onload = () => {
        if (current === version) { logo.src = reader.result; logo.hidden = false; paint(); }
      };
      reader.readAsDataURL(file);
    } catch (e) {
      if (current === version) document.querySelector('#logo-error').textContent = e.message || 'Upload failed. Please try again.';
    }
  });
}

const csrf = () => form.elements.csrfmiddlewaretoken.value;

async function finish(action) {
  const response = await fetch('/signup/branding/', {
    method: 'POST',
    headers: { 'X-CSRFToken': csrf(), 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ action, csrfmiddlewaretoken: csrf() }),
  });
  window.location.assign(response.redirected ? response.url : '/overview/');
}

form.addEventListener('submit', async event => {
  // "Skip for now" is a legitimate way out and must work even when the save
  // below would fail, so it is allowed to post natively.
  if (event.submitter && event.submitter.value === 'skip') return;
  event.preventDefault();
  const button = form.querySelector('button[type=submit]');
  if (button) button.disabled = true;
  try {
    const current = palette();
    const payload = {
      name: form.elements.name.value,
      primary: current.primary,
      secondary: current.secondary,
      accent: current.accent,
      logo_token: form.elements.logo_token.value,
    };
    const response = await fetch('/api/settings/', {
      method: 'POST',
      headers: { 'X-CSRFToken': csrf(), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw Error(result.error || 'We could not save your branding.');
    }
    await finish('finish');
  } catch (e) {
    document.querySelector('#logo-error').textContent = e.message || 'We could not save your branding.';
    if (button) button.disabled = false;
  }
});
})();
