/*
 * The organization theme, in the browser.
 *
 * This is the single browser implementation of the palette rules. Both theme
 * surfaces use it: the Django first-run branding step (signup-branding.js) and
 * the React Settings form (work.tsx, through theme-bridge.ts). Neither surface
 * computes a colour of its own.
 *
 * The authoritative definition of these rules is backend/ledger/branding.py,
 * which is what actually stores the palette and computes the text colours it
 * serves. This file must agree with it. tests/theme-parity.test.mjs runs both
 * over the same fixtures and fails if they ever disagree, so a change to one
 * has to be made deliberately in the other.
 *
 * Colours are plain six-digit hex, lowercase, with a leading '#'. That is the
 * only form the model accepts and the only form the colour inputs produce.
 */
(() => {
  const FIELDS = ['primary', 'secondary', 'accent'];

  // Relative luminance, WCAG 2.x. sRGB is linearized per channel before the
  // weighted sum, which is why the transfer function is not a plain average.
  const channel = value => {
    const srgb = value / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  };

  const rgb = hex => [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16));

  function luminance(hex) {
    const [r, g, b] = rgb(hex);
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  }

  // Contrast ratio between two colours, 1 to 21.
  function contrast(a, b) {
    const first = luminance(a);
    const second = luminance(b);
    return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
  }

  /*
   * Pick the label colour for a filled surface.
   *
   * Decided by comparing the two candidate ratios rather than by testing a
   * rounded luminance threshold, so there is no magic number to drift between
   * this file and branding.py. Whichever of black or white actually contrasts
   * more wins, which is never below 4.5:1 on an opaque colour.
   */
  function textColor(background) {
    const value = luminance(background);
    const onBlack = (value + 0.05) / 0.05;
    const onWhite = 1.05 / (value + 0.05);
    return onBlack >= onWhite ? '#000000' : '#ffffff';
  }

  /*
   * What to warn about.
   *
   * Text placed on a filled surface is handled by textColor and cannot fail, so
   * these are the pairings nothing repairs: two of the chosen colours sitting
   * next to each other, or the primary used as a link on a light surface. They
   * are reported, never corrected. Silently darkening a colour somebody picked
   * would hide the decision, and the product has no such rule.
   *
   * Minimums follow WCAG: 4.5:1 for text, 3:1 for a boundary a user has to
   * tell apart.
   *
   * Accent against primary is deliberately absent. The two meet only at the logo
   * mark, which is a filled shape carrying a letter: it reads as present from its
   * outline, not from its colour, and no control or status depends on telling
   * them apart. Holding it to 3:1 would warn about the palette the product ships
   * with, and a warning that is always on is a warning nobody reads.
   */
  const TEXT_MINIMUM = 4.5;
  const BOUNDARY_MINIMUM = 3;

  const SURFACE = '#ffffff';

  function paletteWarnings(palette) {
    const { primary, secondary, accent } = palette;
    const checks = [
      {
        ratio: contrast(primary, SURFACE),
        minimum: TEXT_MINIMUM,
        message: 'Text links and text buttons use the primary colour. They may be hard to read on white.',
      },
      {
        ratio: contrast(primary, secondary),
        minimum: BOUNDARY_MINIMUM,
        message: 'The selected navigation item is drawn in the secondary colour. It may not stand out from the sidebar.',
      },
      {
        ratio: contrast(secondary, accent),
        minimum: BOUNDARY_MINIMUM,
        message: 'The accent colour and the selected navigation item are used on the same sidebar. They may look too similar.',
      },
    ];
    return checks
      .filter(check => check.ratio < check.minimum)
      .map(check => ({ ...check, ratio: Math.round(check.ratio * 100) / 100 }));
  }

  /*
   * Write the palette onto an element as the six --org-* custom properties the
   * stylesheets read. This is the only place the two representations meet: state
   * in, CSS variables out. Scoping to `target` rather than the document lets the
   * preview drive its own copy without repainting the page behind it.
   */
  function applyTheme(target, palette) {
    FIELDS.forEach(field => {
      const value = palette[field];
      target.style.setProperty(`--org-${field}`, value);
      target.style.setProperty(`--org-${field}-text`, textColor(value));
    });
  }

  const DuesdeskTheme = { FIELDS, luminance, contrast, textColor, paletteWarnings, applyTheme };

  if (typeof window !== 'undefined') window.DuesdeskTheme = DuesdeskTheme;
  if (typeof globalThis !== 'undefined') globalThis.DuesdeskTheme = DuesdeskTheme;
})();
