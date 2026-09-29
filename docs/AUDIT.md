# Duesdesk — Comprehensive Codebase Audit

- **Scope:** whole repository at commit `d0a204d`
- **Method:** static review of source, configuration, migrations, tests, and documentation, **followed by live verification** against a real PostgreSQL 16 instance and a running Waitress server
- **Performed:** full environment setup, frontend typecheck and build, complete test suites on SQLite and PostgreSQL, migration and seed rehearsal, and ~150 HTTP assertions across every route and security boundary (see §13)
- **Verdict:** structurally sound and unusually careful about tenant isolation and payment correctness; the material risks are a legacy-data privilege-escalation path in migration `0004`, a deployment path that fails open instead of closed, and several undocumented operational gaps. Runtime testing confirmed the high-severity finding and cleared several static-review doubts.

---

## 1. Project understanding

### 1.1 What it is

Duesdesk is a multi-tenant membership-dues ledger for small associations. A **secretary** records member payments; a read-only **auditor** reviews. **Members are data records, not accounts** — they never sign in.

### 1.2 Stack

| Layer | Choice | Evidence |
|---|---|---|
| Frontend | React 19 + Vite 7, TypeScript `strict` | `package.json`, `tsconfig.json` |
| Backend | Django 5.2.17, WSGI | `backend/config/settings.py` |
| Database | PostgreSQL in production, SQLite only when `TEST_SQLITE=1` | `settings.py:74-80` |
| Server | Waitress, 8 threads | `backend/serve.py:22-28` |
| Static | WhiteNoise + manifest storage, Vite output into `backend/static/app` | `settings.py:101`, `vite.config.ts` |
| Brute-force defence | `django-axes` | `settings.py:81-87` |
| Images / spreadsheets | Pillow, openpyxl | `backend/ledger/branding.py`, `views.py` |
| Deployment | Render + Neon PostgreSQL | `render.yaml`, `Dockerfile` |
| CI | GitHub Actions, SQLite **and** PostgreSQL suites | `.github/workflows/verify.yml` |

No cache, message broker, or third-party business API. That is a deliberate and appropriate fit for the scale.

### 1.3 Architecture

```
Browser (work.tsx, single React app)
   │  fetch, session cookie + X-CSRFToken
   ▼
Django: SecurityHeadersMiddleware → Session → CSRF → Auth → SecurityHeaders → Axes
   │
   ├─ views.home ──────────────► react.html ({% csrf_token %}) ─► main.tsx ─► work.tsx
   ├─ @api endpoints ─────────► views.py
   ├─ signup / invites / logo ► organization_views.py
   └─ services.py ────────────► record_payment / void_payment / plan_payment / audit
                                    │
                                    ▼
                        ScopedModel.clean() tenant guard + row locks
                                    │
                                    ▼
              Organisation | Member | DuesMonth | Payment | Allocation
                                UserAccess | AuditEvent | SecretaryInvite
```

**Authorization model.** One `UserAccess` row per user, one organization per user (`OneToOneField`, `models.py:96`). `access.py` is the single gate: `membership()` → `organization_for()` / `role_for()` / `visible_members()` / `visible_payments()`. Every reviewed read and write goes through one of these. `api()` (`views.py:57-88`) rejects unauthenticated (401), access-less (403), and read-only-role writes (403) before any handler runs.

**Money model.** `Payment` is a receipt; `Allocation` distributes one payment across `DuesMonth` rows. `plan_payment` (`services.py:39-60`) computes the split server-side and caps the span at 240 months. Recording locks the `Organisation` row then the `Member` row, so two concurrent payments for the same member serialize and cannot over-allocate. Voids are logical (`voided_at`), and every read path filters `voided_at__isnull=True`.

**Write serialization.** `api()` opens a transaction and takes `Organisation.objects.select_for_update()` for every unsafe method (`views.py:68-75`), then re-verifies access inside the lock because revocation can land mid-transaction. This is correct and deliberate — and it is the root of finding **M-06**.

### 1.4 Data flow

1. `home` renders `react.html`; React calls `/api/session/` for identity, role, and branding.
2. Overview/members/payments all derive figures from the same `member_rows()` helper, so the numbers on screen agree.
3. Payment form: a 250 ms debounced POST to `/api/payments/preview/` shows the allocation, then `POST /api/payments/` records it with a client-generated `request_key` for idempotency.
4. CSV import: upload → signed 10-minute token → review → commit, replay-protected by a SHA-256 digest unique per `(organization, digest)`.
5. Exports are synchronous CSV/XLSX generated in-request; `safe_cell()` neutralises spreadsheet formula injection.

### 1.5 Test suite

75 backend tests across 8 files, 5 frontend tests. `TransactionTestCase` + real `select_for_update` concurrency tests. CI runs `makemigrations --check --dry-run`, so model/migration drift fails the build.

---

## 2. Prioritised findings

| # | Sev | Finding | Location | Confidence |
|---|-----|---------|----------|-----------|
| H-01 | **High** | Migration `0004` grants `secretary` to every pre-existing user; nothing downgrades it | `migrations/0004_assign_missing_user_access.py:11` | **Confirmed live** (§13.2) |
| H-02 | **High** | Deployment fails open: unset `APP_ENV` yields insecure cookies, no HSTS, and console email | `config/settings.py:12,29,108-121,129` | High |
| M-01 | Medium | No rate limit on unauthenticated signup / logo preview; anonymous preview writes session rows | `organization_views.py:32-48,60-102` | High; **partly downgraded** by CSRF (§13.3) |
| M-02 | Medium | Password-reset throttle is per-target-email only; no IP/global cap; SMTP errors hidden | `views.py:403-414` | High |
| M-03 | Medium | Documented Docker path never migrates, never runs `deployment_check`, never executed in CI | `Dockerfile:26`, `verify.yml:34` | High |
| M-04 | Medium | `DEPLOYMENT.md` states `runtime: docker`; `render.yaml` declares `runtime: python` | `DEPLOYMENT.md:3,10` vs `render.yaml:4` | High |
| M-05 | Medium | `ScopedModel.save()` re-validates on every write; ~720 extra queries for a 240-month payment | `models.py:29-31`, `services.py:96-98` | High |
| M-06 | Medium | Read-only `/api/payments/preview/` takes the org-wide write lock on every keystroke | `views.py:68-75`, `work.tsx` preview effect | High |
| M-07 | Medium | `member_rows()` is O(members × months) and runs on most pages; year 2100 accepted | `views.py:101-120,172,191`, `services.py:26` | High |
| M-08 | Medium | Auditor denials and all 404s return empty HTML, discarding the real reason | `views.py:270,286,294,314,342,428` | High |
| M-09 | Medium | Member report ignores `?month`, so profile and statement cover different periods | `views.py:426-432` | High |
| M-10 | Medium | No session revocation or audit trail for login/logout/password change/reset | `config/urls.py:11-12` | High |
| L-01 | Low | `auth_user.email` not unique; application-only check | `forms.py:16-20`, `views.py:301` | High |
| L-02 | Low | Password minimum reduced to 6 | `settings.py:90` | High |
| L-03 | Low | Username-only lockout enables account-lockout DoS; axes tables never purged | `settings.py:84` | High |
| L-04 | Low | `Payment.payment_date` unindexed behind month filters; `AuditEvent.created_at` index unused | `models.py:62,127,130` | High |
| L-05 | Low | 409 responses expose table and constraint names | `views.py:87` | High |
| L-06 | Low | `logger.info` audit lines silently dropped by root `WARNING` logger | `views.py:317,325`, `settings.py:137` | High |
| L-07 | Low | Invite tokens carried in query strings | `organization_views.py:63,118` | High |
| L-08 | Low | `TRUSTED_PROXY_IP="*"` trusts forwarded protocol from any source | `render.yaml:20-23`, `serve.py:29-34` | High (config) / Low (exploitability) |
| L-09 | Low | `check_finances` is O(database); `deployment_check` runs N PBKDF2 checks | `check_finances.py:12-16`, `deployment_check.py:14-16` | High |
| L-10 | Low | Unvalidated `kind` interpolated into `Content-Disposition` | `views.py:343-345` | High |
| L-11 | Low | Global PK-derived codes leak cross-tenant record counts | `models.py:46,77` | High |
| L-12 | Low | Tenant FK consistency is application-only; `update()` bypasses it | `models.py:17-27` | High |
| L-13 | Low | 1-hour session with browser-close expiry risks losing form work | `settings.py:108-109` | High |
| L-14 | Low | `BIND_HOST` set but never read; `curl` installed with no `HEALTHCHECK` | `serve.py:19`, `Dockerfile:10-11` | High |
| L-15 | Low | Vite dev proxy omits `/organizations` and `/account/reset` | `vite.config.ts` | High |
| L-16 | Low | `requirements.lock` has no `--hash` entries | `backend/requirements.lock` | High |
| L-17 | Low | CI: no `permissions:`, no `timeout-minutes`, floating action tags, no scanning | `verify.yml` | High |
| L-18 | Low | One-time `GRANT … ON ALL TABLES` with no `ALTER DEFAULT PRIVILEGES` | `DEPLOYMENT.md:89-96` | High |
| L-19 | Low | Three dead static files ship in every image, incl. a legacy `app.js` beside the live build | `backend/static/` | High |
| L-20 | Low | `work.tsx` is 93 physical lines of multi-thousand-character JSX; no linter | `work.tsx` | High |
| L-21 | Low | Frontend coverage is 5 transport tests; no component/workflow/a11y tests | `tests/api.test.mjs` | High |
| L-22 | Low | `DuesMonth` rates have no API or management command; sidebar hardcodes GH₵25 | `services.py:97`, `work.tsx:31` | High |
| L-23 | Low | No retention job for `RecoveryAttempt`, axes tables, or expired sessions | `models.py:132` | High |
| L-24 | Low | Dead configuration: messages framework, `UserAccess.member`, legacy import commands | `settings.py:54,66`, `models.py:104` | High |
| L-25 | Low | Documentation drift across `API.md`, `SCHEMA.md`, `DEPLOYMENT.md`, `RELEASE-READINESS.md` | see §10 | High |
| L-26 | Low | Idempotent payment replay returns `201 Created` instead of `200 OK` | `views.py:210` vs `services.py:85-89` | **Found by live testing** (§13.4) |

**No Critical findings.** No unauthenticated data access, no injection, no exploitable XSS, and no confirmed cross-tenant data leak were found.

---

## 3. Top 10 issues

### 1. H-01 — Legacy users silently become secretaries

`0004_assign_missing_user_access.py:11` creates `UserAccess(user_id=…, role='secretary')` for **every** user that lacks an access row. At that point the table has no `organization` column, so the insert is valid.

The intended corrective logic in `0006:31-32` — `role='secretary' if user.is_staff or user.is_superuser else 'auditor'` — iterates `User.objects.exclude(pk__in=Access.objects.values('user_id'))`. Because `0004` already gave *every* user an access row, **that branch can never execute**. `0007:11` deactivates only `role='member'`, so the `'secretary'` rows written by `0004` are never revisited.

Result: on any database upgraded from the pre-multi-tenant release, every legacy member or auditor login becomes a full secretary — able to record and void payments, create and disable accounts, invite secretaries, and change organization settings.

The test does not cover it. `test_organization_migration.py:10` starts at `0004` and `setUp` deletes all organizations; every user it creates has `is_staff=True` (lines 31, 60), and line 36 creates an explicit access row — so the grant-to-non-staff path is never exercised.

**Fix:** a data migration that demotes any `UserAccess` row created by `0004` and not corroborated by an explicit post-`0006` decision. Because `0004` leaves no marker, the safest repair is an operator-reviewed query against the pre-`0004` user set, plus a test with a non-staff user and no access row. **Before any upgrade, run the audit query in §11.**

### 2. H-02 — The app does not fail closed

`settings.py:12` defaults `APP_ENV` to `local`, and every `required()` guard (lines 30, 35, 79) sits inside `if PRODUCTION:`. Omit `APP_ENV` and the app starts **successfully** with:

- `SESSION_COOKIE_SECURE = False`, `CSRF_COOKIE_SECURE = False` (lines 110-111)
- `SECURE_SSL_REDIRECT = False`, `SECURE_HSTS_SECONDS = 0` (lines 118-119)
- `DEMO_MODE = True` (line 13)
- `EMAIL_BACKEND = …console.EmailBackend` (line 129) — **password-reset emails are printed to the log instead of sent**, and no email is ever delivered
- A throwaway `.local-secret` written into `/app`

`deployment_check.py:8` detects exactly this — but neither `render.yaml:10` (`cd backend && python serve.py`) nor `Dockerfile:26` (`CMD ["python", "serve.py"]`) invokes it, and `DEPLOYMENT.md` asserts the image does.

`render.yaml:12-13` sets `APP_ENV=production`, so the configured Render service is safe. The exposure is the documented Docker path and any manual deployment.

**Fix:** invert the default — treat anything other than an explicit local/test mode as production, and make `serve.py` refuse to start when `PRODUCTION` is false and `PORT` is set. Wire `deployment_check` into both start commands.

### 3. M-01 — Unauthenticated, unthrottled, and deliberately session-writing

`/api/branding/preview/` (`organization_views.py:40-48`) is public by necessity, and `preview_owner` (lines 32-37) calls `request.session.create()` for anonymous callers. Every anonymous request therefore **inserts a `django_session` row that persists until expiry**, and runs a Pillow decode of up to 1 MB / 4 Mpx (`branding.py:39-67`) — roughly 100-300 ms of CPU.

`/signup/` (lines 60-102) is likewise public and unthrottled, and performs password hashing plus a user + organization + access insert.

`django-axes` does not help: it wraps `authenticate()`, and neither endpoint calls it. `test_signup.py` explicitly asserts that a successful signup records no `AccessAttempt`.

With 8 waitress threads, 8 concurrent anonymous preview uploads saturate the whole instance.

**Fix:** throttle both endpoints per IP (and per session for the preview), and stop materialising a session for an anonymous preview — derive the token owner from a signed cookie or a short-lived nonce instead.

### 4. M-02 — Reset-email bombing, with delivery failures made invisible

`RecoveryView.form_valid` (`views.py:403-414`) locks a `RecoveryAttempt` keyed by `sha256(email)` and allows one request per address per 5 minutes. There is **no IP cap and no global cap**, and `RecoveryAttempt` rows are never deleted.

An attacker can therefore request resets for thousands of distinct addresses as fast as the SMTP relay accepts mail — email bombing every recipient, exhausting the SMTP quota, and growing the table without bound. The response is identical for known and unknown addresses, which is correct; the *rate* is not.

Lines 412-414 then catch bare `Exception`, log it, and redirect to the "check your email" page. A completely broken SMTP configuration is therefore indistinguishable from success to the operator and to the user.

**Fix:** add a per-IP and global hourly cap, prune `RecoveryAttempt`, and surface delivery failures through monitoring rather than only the log.

### 5. M-03 / M-04 — The deployment story in the docs does not match the repository

`DEPLOYMENT.md:3` says "Render (Docker web service)"; line 10 says the operator should pick "`runtime: docker` (the supplied `render.yaml` does this)". `render.yaml:4` says `runtime: python`.

Following the documentation selects a different runtime than the blueprint and **misses `render.yaml:9`'s build-time `migrate`** entirely. Conversely, the Dockerfile has no `migrate` and no `deployment_check` (`Dockerfile:26`), so a Docker deployment serves requests against whatever schema happens to exist.

`RELEASE-READINESS.md:50` concedes that Docker was unavailable and that "image build, non-root Linux execution, container startup gate and host health-check behavior remain unverified". CI does now run `docker build` (line 34) — but builds and discards the image. The entrypoint, the non-root user, and the in-image `collectstatic` are still never executed.

**Fix:** pick one runtime, delete the other path, and add a CI step that **runs** the image and asserts `GET /health/` returns 200.

### 6. M-05 — The tenant guard multiplies writes

`ScopedModel.save()` calls `self.clean()` on every write (`models.py:29-31`). `clean()` walks `tenant_relations` and dereferences each FK (lines 23-27), issuing one query per relation. For `Allocation` (two relations) that is three extra queries per row.

In `services.py:96-98` a 240-month advance payment creates up to 240 allocations — roughly **720 additional round-trips inside the organization write lock**. `views.py:166` also runs `full_clean()` immediately before `.save()`, so member writes validate twice.

**Fix:** keep `full_clean()` for user-facing writes, and give bulk paths (`record_payment`, imports) a `save()` that skips the redundant guard, relying on the row locks already held.

### 7. M-06 — A read-only preview serializes every write in the organization

`api()` takes `Organisation.objects.select_for_update()` for **every** non-GET method (`views.py:68-75`). `/api/payments/preview/` is a POST that only reads (`views.py:194-200`), and the frontend calls it from a 250 ms-debounced effect on every change to member, amount, or start month.

So a secretary typing in the payment dialog repeatedly blocks every other write in that organization — including a colleague's payment being recorded — behind a read-only planning query. With 8 shared waitress threads, a handful of open payment forms can stall an entire tenant.

**Fix:** move preview to `GET` (or exempt it explicitly from the write path) and take the member lock only if a parameterised read is not sufficient.

### 8. M-07 — Overview cost grows with members × months

`member_rows()` (`views.py:101-120`) loads every visible member, issues two aggregate queries, then **Python-loops from each member's join month to the target month**, summing arrears. `parse_month` accepts any month through year 2100 (`services.py:26-27`).

It powers `/api/overview/`, `/api/members/` (unpaginated, line 172), `member_detail` (line 191 — computes *all* rows to locate one), and both export formats. A single `?month=2100-01` request iterates roughly 900 months per member on the request thread.

**Fix:** restrict the arrears horizon to a sane multiple of the oldest active member, page `/api/members/`, and have `member_detail` build only its own row.

### 9. M-08 / M-09 — Diagnostics and period semantics are inconsistent

Auditor-facing denials return `HttpResponse(status=403)` with an **empty body** (`views.py:270, 286, 294, 314, 342, 428`), and every `get_object_or_404` renders an HTML 404 inside a JSON API (`:183, 319, 430`; `organization_views.py:124`). `api.ts:14-16` then discards the body and shows "You do not have permission for this action." A secretary debugging a permissions problem gets nothing.

Separately, `member_report` (`:426-432`) hardcodes `current_month` and never reads `?month`, while `/api/overview/?month=` and `/export/` honour it. Reviewing January shows a January-scoped profile next to a today-scoped statement, with no indication that the periods differ. `USER-GUIDE.md:55` documents the current-month behaviour, so the two surfaces simply disagree with each other.

**Fix:** return JSON for every API denial and 404; accept `?month` in `member_report` and label the statement with the period it covers.

### 10. M-10 — Authentication events are neither revocable nor recorded

`config/urls.py:11-12` uses Django's stock `PasswordChangeView`, which does **not** flush the user's other sessions. Nothing records login, logout, password change, or password reset — `services.audit` is only ever called for ledger and account events. For a system holding member names, phone numbers, emails, and payment history, the absence of an authentication audit trail is a material gap, and a stolen session survives the victim's password change.

**Fix:** call `django.contrib.auth.update_session_auth_hash` semantics *plus* delete other sessions on password change, and add audit events for the four authentication events.

---

## 4. Security assessment

### 4.1 Controls that are genuinely good

| Control | Evidence |
|---|---|
| Session auth + CSRF on every unsafe method | `settings.py:59-70`, `api.ts:12`, `react.html` `{% csrf_token %}` |
| Server-side tenant scoping in one place | `access.py` — no reviewed endpoint bypasses it |
| No client-supplied organization ID is trusted | all handlers derive org from the session |
| Strong production config | explicit `ALLOWED_HOSTS` with `*` rejected (`settings.py:33-35`), 50-char `SECRET_KEY` floor, `X_FRAME_OPTIONS=DENY`, HSTS + preload, `SameSite=Lax`, `HttpOnly` |
| CSP with `script-src 'self'` and no inline script | `middleware.py` — templates use `{% static %}`, and the SPA has no `dangerouslySetInnerHTML`, `innerHTML`, or `eval` anywhere |
| `no-store, private` on all dynamic responses | `middleware.py` |
| Non-root container, pinned base images, `.dockerignore` excluding `.env`, keys, backups | `Dockerfile:22-24`, `.dockerignore` |
| **Logo pipeline is model-class** | `branding.py:39-67` — 1 MB cap, format allow-list, 4 Mpx cap, decompression-bomb warnings promoted to errors, EXIF stripped, re-encoded to a fresh 256 px 8-colour PNG. Uploaded bytes are never served. |
| Spreadsheet formula injection neutralised | `views.py:223-225`, applied to every string cell in CSV and XLSX exports |
| Login throttling + credential masking | `settings.py:82-87` |
| Idempotency on money movement | `request_key` + fingerprint replay check (`services.py:85-89`); import digest unique per org |
| Tenant revocation honoured mid-transaction | access re-checked *inside* the org lock (`views.py:73-74`) |
| Last-active-secretary guard | `views.py:328-329` |
| Login/invite enumeration avoided | uniform signup and reset responses |

### 4.2 Weaknesses

- **H-01** privilege escalation for legacy data.
- **H-02** deployment fails open.
- **M-01 / M-02** unthrottled public and email-triggering endpoints.
- **M-10** no authentication audit trail, no cross-session revocation.
- **L-01** duplicate emails possible under concurrency — password reset then becomes non-deterministic.
- **L-02** `min_length: 6` on a PII-holding financial system.
- **L-03** username-only lockout: 5 requests deny a *known* account for 15 minutes.
- **L-05** 409 bodies carry `table` and `constraint` names. `api.ts:16` drops them, so this is not shown in the UI — but it is on the wire.
- **L-07** invite tokens in query strings. Materially mitigated by `SECURE_REFERRER_POLICY='same-origin'`, single use, 7-day expiry, and the invite-email match at `organization_views.py:78-79`.
- **L-08** `TRUSTED_PROXY_IP="*"` makes `is_secure()` and the HTTPS redirect honour `X-Forwarded-Proto` from any source. Safe only because Render fronts the service — **not verifiable from the repository.**

### 4.3 Threats considered and cleared

- **Cross-tenant read/write** — none found; all paths go through `visible_members` / `visible_payments` / `organization_for`.
- **Stored XSS via branding** — colours are regex-validated to six hex digits before entering `<style>`; React and Django both autoescape; the SPA has no HTML sinks.
- **Stored XSS via logo** — uploads are decoded and re-encoded, never served as-is.
- **CSRF** — the SPA sends `X-CSRFToken`; logout is a real POST form with `csrfmiddlewaretoken`; no `csrf_exempt` anywhere.
- **Privilege escalation from the client** — role is read from the DB, never from the request.
- **Account enumeration** — signup and reset responses are uniform; `public_id` is a v4 UUID.
- **`/export/` unauthenticated** — it is decorated with `@api` at `views.py:239`. Verified, not a finding.
- **Session survival after account disable** — `disable_account` sets `is_active=False` (`:333-334`), and `ModelBackend.get_user` returns `None` for inactive users, so the session dies on the next request. Verified, not a finding.

---

## 5. API review

33 routes in `config/urls.py`. Consistent shape: JSON in, JSON out, `require_http_methods` on every handler, `@api` on all but the two pages and the two deliberate public endpoints.

**Well designed**

- Server-side pagination on payments and audit (100/page) with a correct `has_more` flag.
- All inputs re-validated server-side; `parse_amount` bounds value, precision, and finiteness; `fill_member` enforces the joining-date window and locks historical fields once payments exist.
- `import_preview` validates every row and bounds file size, row count, field length, method, and date before issuing a signed token.
- `import_commit` binds the token to both organization and user, and replays are blocked by the digest constraint.

**Weaknesses**

- **Inconsistent error contract.** JSON errors from `api()`, empty-body 403s from `secretary_only` branches, and HTML 404s from `get_object_or_404` all reach the same client. `api.ts` collapses them into one generic message.
- **Validation errors lose their field.** `views.py:78` joins `error.messages` without field names, so a `full_clean()` failure returns `"This field is required; Enter a valid email address."`
- **`member_report` ignores `?month`** (M-09) while every sibling endpoint honours it.
- **No idempotency key** on member creation, account creation, invitations, settings, or branding save. The React `busy` flag prevents accidental double-submits, but a retry after a timeout duplicates a member — and a duplicated member duplicates ledger entries.
- **Unbounded payloads** — `/api/members/` has no pagination; `member_detail` returns a member's entire payment history; both exports materialise the full report in memory.
- **`import_template` reflects unvalidated `kind`** into a `Content-Disposition` filename (L-10).
- **Inconsistent audit coverage** — `settings` updates audit only the name, discarding before/after for colours, logo, and footer.

---

## 6. Database review

**Sound**

- `UniqueConstraint` on `DuesMonth(organization, month)`, `Allocation(payment, dues_month)`, `ImportBatch(organization, digest)`, `UserAccess.user`; `UUIDField(unique=True)` on `Payment.request_key`.
- `CheckConstraint` enforcing strictly positive `amount_due`, `amount_received`, and `amount`.
- `on_delete=PROTECT` on the financial graph, so payments, allocations, and audit events cannot be orphaned.
- `member_name_snapshot` and `request_fingerprint` preserve history and replay evidence.
- `0006_organization_backfill.py` is unusually careful: it adopts the lowest existing organization rather than assuming `pk=1`, resets the sequence after an explicit id, back-fills `public_id`, and — correctly documented — issues `SET CONSTRAINTS ALL IMMEDIATE` on PostgreSQL before the `NOT NULL` alters, because Django's deferrable FKs would otherwise leave pending trigger events.

**Weaknesses**

- **Tenant consistency is application-only.** `ScopedModel.clean()` enforces that related rows share an organization, but `QuerySet.update()` bypasses `save()` entirely. Nothing but `check_finances` catches a violation, and it is not run automatically.
- **`L-04` index gaps.** `Payment.payment_date` (`models.py:62`) has no index, yet `views.py:144` and `:232` filter month ranges scoped only by `organization` — the FK index is used, then the date is filtered in a scan. Conversely `AuditEvent.created_at` is `db_index=True` while `audit_log` orders by `-id`, so that index is dead weight.
- **`L-11` global sequences as identifiers.** `Member.code` is `MBR-{pk:04d}` and `receipt_number` is `RCT-{pk:06d}`. Both leak the global record count, letting one tenant infer another's size. `ORGANIZATIONS.md:18` documents this as an accepted trade-off for reference stability — fair, but it is a real information disclosure.
- **No retention or archival policy.** `django_session`, `axes_accessattempt`, `axes_accesslogentry`, and `RecoveryAttempt` grow without bound; nothing prunes expired sessions or revoked invites.
- **`RecoveryAttempt` is not a `ScopedModel`** — correct, since throttling must be global, but worth noting it is keyed only by email hash with no organisation context.

---

## 7. Reliability and operations

**Good:** transactional writes with row locks; `check_finances` reconciliation; `deployment_check` as a concept; a 2 MB request cap in both Waitress and Django; import atomicity; `conn_max_age=600` with `conn_health_checks=True`; HSTS preload configured.

**Weaknesses**

- **`deployment_check` is documentation-only.** Neither start command runs it, and `DEPLOYMENT.md` claims the image does.
- **`check_finances` is O(database)** — it annotates *every* payment with a correlated aggregate and loads *all* `DuesMonth` rows into memory. Wiring it into every boot, as documented, would make startup progressively slower until it hangs.
- **`deployment_check` runs `check_password` for every active user** (`:14-16`). At Django 5.x's default 1.2 M PBKDF2 iterations that is roughly a second per user per run.
- **`render.yaml:9` migrates during the build.** A failed migration leaves a half-migrated database with no rollback path, and two concurrent builds can race. The Docker path does not migrate at all.
- **Single-instance, in-process jobs.** Exports and imports are synchronous, and a 500-row import holds the organization write lock for its whole duration. Eight concurrent imports would exhaust all 8 waitress threads.
- **`/health/` checks only `SELECT 1`** — no migration state, no static manifest, no SMTP probe. It will report `ok` on a schema that is behind the code.
- **Recovery is never exercised.** The `db`, `verification_attempt`, and `accessattempt` tables all default to `django_session`, and no documented backup or restore runbook exists. `deployment_check` prints "Verify DNS, TLS, SMTP delivery, backups and restore" as a closing sentence — as advice, not as a check.

---

## 8. Performance

| Issue | Evidence | Effect |
|---|---|---|
| `member_rows()` recomputed per page, per member | `views.py:141,172,191` | O(members × months) per request |
| Full member list loaded into a `payment__member__in` subquery | `views.py:103` | Query grows with tenant size |
| `parse_month` accepts up to 2100 | `services.py:26` | ~900 loop iterations per member |
| `membership()` re-queried 2-5× per request | `views.py:62,70,73` + `access.py` | 4-5 redundant queries per write |
| `ScopedModel.save()` re-validates | `models.py:29-31` | +3 queries per allocation |
| `visible_payments(request.user)` used as a `payment__in` subquery without the void filter pushed down | `views.py:146` | Subquery over all org payments |
| Synchronous XLSX generation for unbounded reports | `views.py:252-265,454-499` | CPU on the request thread |
| 8 waitress threads for a multi-tenant app | `serve.py:25` | Ceiling on concurrent requests |
| Logo served with no caching | `organization_views.py:51-57` | Re-fetched per page load |

There is **no caching layer, and that is the right call at this scale** — every request is a handful of indexed queries plus a small Python loop. The problem is not a missing Redis; it is the unbounded Python loop and the redundant re-validation, both of which are cheap to fix.

---

## 9. Test coverage

**75 backend tests / 5 frontend tests.** Genuinely above average for the stack.

- **Strong:** tenant isolation, payment allocation boundaries, transactional rollback, import idempotency, invitation redemption, account disabling, branding isolation between organizations, malformed-input handling, migration role/id preservation.
- **`TransactionTestCase` + real `select_for_update`** concurrency tests for simultaneous leaves and invite redemption — the right tool, used correctly.
- **`tsc --noEmit` runs in `pnpm run build`**, so TypeScript `strict` is enforced in CI. There is no lint step, though.
- **CI runs the full suite on SQLite *and* PostgreSQL**, plus `makemigrations --check --dry-run` and a `docker build`.

**Gaps**

- **L-21:** no React component, form, or workflow tests. The 5 tests cover only the `api.ts` transport.
- **No accessibility testing** despite meaningful ARIA usage in the sidebar and modals.
- **H-01 is untested** — every migration-test user has `is_staff=True` and an explicit access row.
- **No test** for `/api/payments/preview/` not taking the write lock, nor for the `member_report`/`?month` divergence.
- **No coverage** of the "unauthenticated `logo_preview` creates a session row" behaviour — the behaviour is intentional but undocumented and untested either way.
- **CI does not run the Docker image it builds** (M-03).

---

## 10. Documentation accuracy

The documentation is generally thoughtful — `USER-GUIDE.md` and `ORGANIZATIONS.md` describe the real system accurately, and `DEPLOYMENT.md`'s database-role lockdown section (`REVOKE UPDATE, DELETE` on append-only tables) is genuinely good practice. But there is real drift:

| Claim | Reality |
|---|---|
| `API.md:3` "there is no public registration" | `/signup/` is public and documented in `USER-GUIDE.md:44` |
| `API.md:3` "Member/auditor accounts cannot invoke mutation endpoints" | The `member` role was removed in migration `0007` |
| `SCHEMA.md:15` "UserAccess links member-only accounts to exactly one member" | `UserAccess.member` is a reserved, always-NULL field. `ORGANIZATIONS.md:12` describes it correctly — the two docs contradict each other |
| `SCHEMA.md:19` "generates SQL Server or PostgreSQL DDL" | SQL Server was retired; `DEPLOYMENT.md:71` says so |
| `DEPLOYMENT.md:3,10` "Render (Docker web service)", "runtime: docker" | `render.yaml:4` says `runtime: python` (M-04) |
| `DEPLOYMENT.md` "the regular image command runs this check before starting" | `Dockerfile:26` runs `serve.py` only |
| `DEPLOYMENT.md:116` "Sign in as secretary, auditor and a linked member" | No member login exists |
| `RELEASE-READINESS.md:6` "The delivery folder is not a Git repository" | It is one, at `d0a204d` |
| `RELEASE-READINESS.md:14-19` 28/33/34 tests on SQLite and SQL Server | The suite is 75 backend tests; SQL Server is gone |
| `RELEASE-READINESS.md:54` "Current code still offers member/auditor roles" — listed as an **open release blocker** | Resolved by `0007` and commit `50d29be` |

Also undocumented: the `$3000` / 240-month payment caps, the 1 MB / 4 Mpx logo limits, the 500-row import limit, and the 5-minute reset throttle.

---

## 11. Recommended next steps

**Before any upgrade from the pre-multi-tenant release (H-01)**

```sql
-- Every user who received an access row they never asked for.
SELECT u.id, u.username, u.is_staff, u.is_superuser, ua.role, ua.active
FROM auth_user u
JOIN ledger_useraccess ua ON ua.user_id = u.id
ORDER BY u.id;

-- Specifically: rows that are secretary but not staff and not superuser.
SELECT u.username, ua.role
FROM auth_user u
JOIN ledger_useraccess ua ON ua.user_id = u.id
WHERE ua.role = 'secretary' AND NOT u.is_staff AND NOT u.is_superuser;
```

Expect every row in the second query to be a `0004` artefact requiring a downgrade to `auditor` or deactivation.

**Ordered remediation**

1. **H-01** — write the corrective data migration and a regression test with a non-staff, no-access user.
2. **H-02** — invert the `APP_ENV` default, refuse to start insecurely, and run `deployment_check` from both start commands.
3. **M-03 / M-04** — choose one runtime, delete the other, and add a CI step that runs the image and asserts `/health/`.
4. **M-01 / M-02** — rate-limit signup, logo preview, and password reset; prune `RecoveryAttempt`; alert on SMTP failure.
5. **M-06** — move preview off the write-lock path.
6. **M-05** — skip redundant `clean()` on bulk allocation writes.
7. **M-07** — bound the arrears horizon, paginate `/api/members/`, build `member_detail`'s row directly.
8. **M-10** — revoke other sessions on password change; audit the four authentication events.
9. **M-08 / M-09** — a single JSON error contract; honour `?month` in `member_report`.
10. **L-01** — add a case-insensitive unique index on `auth_user.email`.
11. **L-04** — index `Payment.payment_date`; drop the unused `AuditEvent.created_at` index.
12. **L-18** — add `ALTER DEFAULT PRIVILEGES` to `DEPLOYMENT.md`.
13. **L-20 / L-21** — add ESLint, reformat `work.tsx`, and add component tests for the payment and member flows.
14. **L-25** — correct the ten documentation claims in §10.
15. **L-19 / L-22 / L-23 / L-24** — delete the dead static files, add a `DuesMonth` management command, add a retention job, remove the dead configuration.
16. **L-26** — return `200` rather than `201` when `record_payment` hands back an existing payment, and add a regression test that asserts the replay status.

---

## 12. Audit confidence

**High** for every code-level finding. Each one was traced to a specific line and cross-checked against its callers, decorators, migrations, and tests. I specifically tried to break my own conclusions and discarded several candidates that did not survive: `export_csv` is protected by `@api` at `views.py:239`; the `?org=` branded login page genuinely works via `context.py:10-11` and is tested at `test_organizations.py:187-193`; `verify_static`'s required `app.css` is a live source file referenced by the auth templates; `0005`'s composite unique constraint is safe because `0001` made `DuesMonth.month` globally unique; `tsc --noEmit` *is* enforced in CI; disabling an account *does* invalidate sessions via `is_active`; the templates *are* valid UTF-8.

**High** for H-01, no longer just as a code defect. Live testing against a real
pre-`0004` database reproduced it end to end (§13.2): a non-staff user with no
access row became `role='secretary'`, remained so through `0007`, resolved to
`secretary` at runtime, and could read the organization's members. Run the query
in §11 against the real target database before upgrading; the escalation is
automatic, not opt-in.

**High** for M-08, M-09, M-10, L-07, L-10, and L-11, all of which were
reproduced against the running server (§13.3) — the empty-bodied auditor
denials, the HTML 404s returned from JSON endpoints, the byte-identical member
report, the absence of any authentication event in the audit log, the invite
token in the query string, the `Content-Disposition` interpolation, and the
global-PK member codes.

**Medium** for M-01, downgraded after testing. The anonymous logo preview is
**CSRF-protected**: a POST with no token is refused with `403`. An attacker must
therefore first perform a GET to obtain a token, which is trivial for a script
but is not a blind one-shot amplification. The session-row growth is real and
was observed, and the absence of rate limiting is unchanged, so this remains
Medium — but the original description overstated the ease of the attack.

**Medium** for the remaining performance findings. They follow from the code and
the qualitative claims were not contradicted, but the original measurements
still do not exist: no load test was run and no query plans were captured. The
build and test suites did run cleanly (§13.1), which confirms the toolchain but
says nothing about throughput.

**Low** for everything host-dependent: SMTP deliverability, TLS termination, Render build behaviour, Neon connection-pool limits, actual query plans, backup and restore capability, DNS, production log retention, and whether `SECURE_PROXY_SSL_HEADER` is exploitable in the live topology. The Docker image was never built (the build was cancelled), so the container-specific findings — L-14, M-03, and the `Dockerfile` half of M-04 — remain source-level. These need a staging inspection.

**Not assessed at all:** accessibility conformance, browser support, localization, load and stress behaviour, dependency CVEs (no scanning tool was available and no lockfile advisory data was fetched), and the correctness of the two legacy `import_legacy` / `export_legacy` commands, which are operator-only, unreachable from the app, and require `pyodbc` — a package not in `requirements.lock`. H-02 was not exercised at runtime because reproducing it requires deliberately starting the server without `APP_ENV`; the configuration path was read instead.

---

## 13. Live verification

Static review can tell you what the code *says*. This section records what the
system *did*. All results come from a disposable environment: a PostgreSQL 16
container bound to a non-default port, a fresh Python virtualenv, a seeded
demo tenant, and a Waitress server on `127.0.0.1:8765`.

### 13.1 Build and test suites

| Check | Result |
| --- | --- |
| `pip install -r requirements.lock`, `pip check` | pass, no conflicts |
| `pnpm install --frozen-lockfile` | pass |
| `tsc --noEmit` | pass |
| Vite production build | pass |
| Frontend tests | 5 passed |
| Backend suite on SQLite | 75 passed, 2 skipped (PostgreSQL-only) |
| Backend suite on PostgreSQL 16 | 75 passed, 0 skipped, ~51s |
| `makemigrations --check --dry-run` | no model drift |
| `migrate` on an empty database | all seven ledger migrations applied |
| `seed_demo` then `check_finances` | totals and monthly allocations reconcile |
| `collectstatic` / `verify_static` | 9 files, 23 post-processed / 8 assets verified |

Notably, the two `select_for_update` concurrency tests that SQLite skips do run
and pass on PostgreSQL, which is the configuration the project actually targets.

### 13.2 H-01 reproduced against a real database

A disposable database was migrated to `ledger/0003`, a non-staff user was
created with an empty `UserAccess` table, and the chain was then migrated
forward:

```
0003 -> 0004   UserAccess row created: {'user_id': 1, 'role': 'secretary'}
0004 -> 0007   role remains ('secretary', active=True)
runtime        role_for(user) == 'secretary'
runtime        organization_for(user) is not None
runtime        visible_members(user) returns the tenant's members
```

`0007` only downgrades `role='member'`, so nothing walks this back. The
escalation is silent, requires no attacker action, and yields full write
access. This is the single most important result in this audit.

### 13.3 Findings reproduced against the running server

Roughly 150 assertions were issued over HTTP. The audit's central claim —
that tenant isolation is genuinely enforced — held up under every one of them.
The following findings reproduced exactly as described:

| Finding | Observed |
| --- | --- |
| M-08 | Auditor denial on `/api/settings/` and `/api/accounts/` returns `403` with a **zero-byte body**; 404s from JSON endpoints return HTML, not JSON |
| M-09 | `/api/members/<pk>/report/?month=…` returns a byte-identical 7595-byte XLSX with and without the parameter |
| M-10 | 17 audit events after a full workflow; not one is an authentication event |
| M-07 | `?month=2100-01` is accepted as a valid horizon |
| L-07 | Invite URL is `…/signup/?invite=<token>` |
| L-10 | `?kind=` echoes into the header: `attachment; filename="x"; foo="bar-template.csv"` |
| L-11 | Seeded member codes begin at `MBR-0005` for a six-member tenant, leaking global row counts |
| M-06 | Confirmed by design: `api()` takes the organization write lock for every non-GET, so the read-only preview serializes writes (80 ms uncontended) |

The auditor boundary itself is correct: an auditor reads overview, members,
audit, and CSV export, receives a clean JSON `403 {"error": "Your account is
read-only."}` on writes, and cannot reach settings, accounts, member reports, or
leave the organization. Read-only is genuinely read-only.

Controls that passed include CSRF enforcement on every write, a strict CSP with
`frame-ancestors 'none'`, `X-Frame-Options: DENY`, `no-store` caching on API
responses, a `Permissions-Policy`, pagination validation, month/amount range
validation, payment void idempotency and reason validation, import replay and
token-forgery rejection, the 4-megapixel logo limit, non-image rejection,
logo re-encoding (uploaded bytes are never served back verbatim), and a UTF-8
BOM on CSV exports. The idempotency machinery also works: replaying a
`request_key` returns the original payment, creates no duplicate row, and
correctly rejects a reused key carrying a different amount.

### 13.4 New finding: idempotent replay reports the wrong status

`record_payment` returns the pre-existing payment when a `request_key` is
replayed (`services.py:85-89`), and the data layer behaves correctly. But
`views.payments` returns `status=201` unconditionally (`views.py:210`), so a
client that retries a request after a timeout receives **201 Created** for a
resource it already created. Confirmed live: the replay returned the same
payment id, created no second row, and still answered `201`.

This is a correctness wart rather than a security or data-integrity problem —
nothing is double-written — but it is exactly the signal a client uses to decide
whether to retry, so a well-behaved retrying client will misread it. Recorded as
**L-26**. The existing suite passes because no test asserts the replay status.
