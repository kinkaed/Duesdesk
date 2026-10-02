(() => {
const form = document.querySelector('#signup-form');
const upload = document.querySelector('#logo-upload');
if (!form) return;

const theme = window.DuesdeskTheme;
const DEFAULTS = { primary: '#214f43', secondary: '#edf4e6', accent: '#527735' };
const fields = theme ? theme.FIELDS : ['primary', 'secondary', 'accent'];

// An invited user is joining an organization that already has a theme, so the
// page shows no colour controls and there is nothing here to repaint. Without
// this the first paint would read a field that is not on the page.
if (!form.elements.primary) return;

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

/*
 * The sign-in background, the register button and the preview are all reading
 * the same six --org-* custom properties. Setting them on the document themes
 * the page itself, which is the point: the chosen colours are already the ones
 * this page is drawn in.
 *
 * textColor is the same rule branding.py applies when it stores a palette, so a
 * colour that reads well here reads well in the finished workspace. applyTheme
 * and paletteWarnings come from theme.js rather than being restated, so the
 * browser and the server cannot drift on what counts as readable.
 */
function paint() {
  const current = palette();
  theme.applyTheme(document.documentElement, current);
  fields.forEach(k => { if (hexLabels[k]) hexLabels[k].textContent = current[k]; });
  const mark = document.querySelector('#theme-preview-mark');
  if (mark) {
    const logo = document.querySelector('#logo-preview');
    if (logo && !logo.hidden) {
      // The uploaded logo replaces the default tile so the preview shows the
      // mark that will actually be stored.
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
    // Text, not a colour swatch: a warning has to survive a screen reader and
    // must not depend on hue to be understood.
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

if (!upload) return;

let version = 0;
upload.addEventListener('change', async () => {
  const current = ++version;
  const submit = form.querySelector('button[type=submit]');
  form.elements.logo_token.value = '';
  document.querySelector('#logo-error').textContent = '';
  const logo = document.querySelector('#logo-preview');
  logo.hidden = true;
  const file = upload.files[0];
  submit.disabled = !!file;
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
    // The server's extracted palette wins, because it is the one that will be
    // stored. Painting afterwards keeps the hex labels and any warnings honest.
    fields.forEach(k => { form.elements[k].value = result[k]; });
    paint();
    const reader = new FileReader();
    reader.onload = () => {
      if (current === version) { logo.src = reader.result; logo.hidden = false; paint(); }
    };
    reader.readAsDataURL(file);
  } catch (e) {
    if (current === version) document.querySelector('#logo-error').textContent = e.message || 'Upload failed. Please try again.';
  } finally {
    if (current === version) submit.disabled = false;
  }
});
})();
