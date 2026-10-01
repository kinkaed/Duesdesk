# The workspace theme

An organisation picks three colours. They are stored on the `Organisation` row
(`backend/ledger/models.py`) as six-digit hex, validated by `branding.color`, and
published to the browser as six CSS custom properties.

| Property | Where it reads | Default |
| --- | --- | --- |
| `--org-primary` | sidebar, primary buttons, links | `#214f43` |
| `--org-secondary` | selected nav item, avatar, workspace tag | `#edf4e6` |
| `--org-accent` | logo mark, sidebar item hover | `#527735` |
| `--org-*-text` | the readable foreground for each of the above | computed |

Those six variables, set in `branding.branding_json`, are the entire contract.
Nothing else in the stylesheet holds an organisation colour, and the sign-up
preview, the settings preview and the running workspace all read the same six.

## One definition of readable

Choosing black or white text on a filled colour happens in two languages:
`branding.text_color` when a palette is stored, and `DuesdeskTheme.textColor` in
`backend/static/theme.js` when the sign-up page repaints live. `theme.js` picks by
comparing the two candidate ratios rather than testing a rounded threshold, so
the two agree by construction and there is no magic number to drift.

`tests/theme-parity.test.mjs` runs both implementations over the same colours and
asserts they return identical results. It also asserts that the palette the
product ships with produces no warnings, because a warning that is always on is a
warning nobody reads.

`work.tsx` no longer computes contrast at all. It reads the `*_text` values that
`branding_json` already sends.

## What is warned about, and what is not

`branding.palette_warnings` checks three pairings and reports what it finds. It
never corrects anything: silently darkening a colour somebody chose would hide a
decision they made, and no such product rule exists.

- primary against white — 4.5:1, because links and text buttons use it
- primary against secondary — 3:1, the selected nav item against the sidebar
- secondary against accent — 3:1, two colours used on the same sidebar

**Accent against primary is deliberately not checked.** They meet only at the logo
mark, a filled shape carrying a letter that reads as present from its outline
rather than from its colour, and no control or status depends on telling them
apart. Requiring 3:1 would fail the palette the product ships with: the default
accent sits at 1.79:1 against the default primary. That ratio was not designed,
it is the result of pointing the logo mark at `accent` when the base stylesheet
still painted it with `--lime`, which sat at roughly 8.9:1 on `--green`. The
change is worth flagging to whoever picks the next default.

Text on a filled surface is not in this list either. `text_color` resolves it by
comparison, and the better of black and white always reaches at least 4.5:1 for
opaque sRGB, so it cannot fail and warning about it would be noise.

## Semantic colours are not themeable

Payment status, audit severity and error states keep their own colours
(`--ok*`, `--warning*`, `--danger*`, `.badge.*`, `.audit-rail.severity-*`,
`.alert`, `.success-mark`). A failure has to look like a failure in every
workspace, or the one screen somebody reads when something is wrong becomes the
screen whose meaning depends on a branding choice.

Theme reach was widened so that cards, tables, links, inputs and focus rings read
the palette. The widening was deliberately narrow: `--green` is aliased to
`--org-primary`, which is a no-op at the default because `--green` and
`DEFAULTS['primary']` are the same value. The default palette therefore looks
exactly as it did before.

## The preview is the real interface

Both previews draw the application, using its own class names, fed by the same
six variables the running workspace uses. Nothing in a preview paints a colour
and nothing is connected to a record. The sign-up preview's rows come from
`PREVIEW_MEMBERS` in `organization_views.py`, a literal, precisely so that a
preview can never read a real member.

`styles.css` is loaded on the sign-up page rather than a copied subset, so the
preview cannot drift from the application. That means the page loads
`theme-preview.css` afterwards to re-assert the auth rules inside `.login-card`
that the application sheet overrides, and it means the page loads the Vite build
output in production (`app/style.css`, hashed through the manifest) and the
source in development. `site_context.app_stylesheet` picks which.

The preview is never load-bearing. With JavaScript off, the three colour inputs
are still plain named POST fields and the form still submits.