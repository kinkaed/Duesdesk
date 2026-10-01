# Duesdesk production-readiness audit

Living document. Supersedes `docs/AUDIT.md` (scoped to commit `d0a204d`, retained for history).

**Audit date:** 2026-09-30 · **Commit audited:** `1f01894` · **Status:** findings only, no code changed

Evidence convention: every claim carries `file:line`. Claims marked **[verified]** were read
directly during this audit. Claims marked **[unverified]** came from delegated research and must be
confirmed against source before any remediation is scoped. Two delegated claims were checked and
**did not hold** — they are recorded in "Retracted claims" so they are not re-investigated.

---

## 1. System overview

Single-tenant-per-user dues ledger. A Django 5.2 monolith owns auth, data, and APIs; a React 19 /
Vite 7 SPA is served from `STATIC_ROOT` under `BASE=/static/app/`.

- **Identity:** `User` (Django auth) → `UserAccess` (OneToOne, carries `organization` + `role`).
  Roles are `secretary` and `auditor`. Members are *records*, not accounts.
- **Money:** `Payment` → `Allocation[]` → `DuesMonth`. Balances are derived from allocations.
- **Tenancy:** enforced by `organization_for()` / `membership()` in `backend/ledger/access.py`.
- **Auth methods:** password (Axes-throttled) and Google OAuth2 via allauth.
- **Deployment:** Render web service, Neon PostgreSQL, `preDeployCommand` runs `migrate` +
  `deployment_check`.

Trust boundaries that matter: unauthenticated internet → session establishment (login, Google
callback, signup, password reset); secretary → organization-wide financial writes; auditor →
read-only. The Google callback is the widest boundary because it is the only unauthenticated path
that grants an existing account's privileges.

---

## 2. Heuristic catalogue

Each heuristic is a testable rule for *this* system, not a generic checklist.

| # | Heuristic | Status | Evidence |
|---|---|---|---|
| H1 | Every unauthenticated credential path is rate-limited per source and per identifier | **PARTIAL** | Signup cap bypass **closed and measured**: 8 POSTs produced 8 emails against a 5/hour cap because `create()` deleted the row holding `code_sends` (`signup.py:107-138`); covered by `SignupSendCapTests`. Google login/callback still have no per-source throttle; Axes is wired only to the password form (`google_auth/forms.py:9-16`) **[verified]** |
| H2 | A payment write is idempotent and cannot be duplicated by a retry | **PASS** | `request_key` unique (`models.py:66`); replay returns existing, fingerprint mismatch rejects (`services.py:203-218`) **[verified]** |
| H3 | `SUM(Allocation)` for a payment always equals `Payment.amount_received` | **PARTIAL** | Enforced in `check_finances.py:17` and app code only; no DB constraint can span tables (`models.py:80,90`) **[verified]** |
| H4 | No authenticated user can read or write another organization's data | **PASS** | Org filter repeated on direct-PK lookups, 404-not-403 to prevent probing (`views.py:605-617`), name resolution re-filtered (`views.py:429-432`) **[verified]** |
| H5 | Disabling an account revokes its live sessions immediately and permanently | **PASS** | `revoke_sessions()` deletes `django_session` rows by decoded `_auth_user_id` (`access.py:19-83`), called from `disable_account` (`views.py:711-716`); tests prove the old cookie stops working and re-enabling does not revive it **[verified]** |
| H6 | Linking an external identity never silently converts an existing account | **FAIL** | Existing account matched by verified email and linked with no owner-consent step (`adapters.py:162-166, 179-188`). **Remediation deferred — needs a product decision; see [`DESIGN-F1-GOOGLE-LINKING.md`](DESIGN-F1-GOOGLE-LINKING.md)** **[verified]** |
| H7 | Every security-relevant state change writes an audit event | **PASS** | CSRF refusals are now written by the `CSRF_FAILURE_VIEW` wrapper (`middleware.py:110-150`); the two events with no legitimate producer were removed from the allowlist and taxonomy, and startup is recorded to the technical log instead (`serve.py:56-71`) **[verified]** |
| H8 | The audit trail is append-only and cannot be edited through the app | **PASS** | Instance + QuerySet guards (`models.py:179-185`, `models.py:128-132`); DB-level `REVOKE` is a documented manual step only **[verified]** |
| H9 | Actor and IP in an audit row are server-derived, never client-supplied | **PASS** | Actor forced from `request.user` (`services.py:44`); XFF deliberately not read (`observability.py:160-166`) **[verified]** |
| H10 | No secret reaches browser JS, logs, errors, or audit details | **PASS** | No `define`/env inlining in Vite build; name-based recursive redaction (`observability.py:41-45, 197-200`) **[verified]** |
| H11 | User-supplied values never reach an HTML sink | **PASS** | No `dangerouslySetInnerHTML` / `innerHTML` in the React app **[verified]** |
| H12 | The UI never acts as the only authorization gate | **PASS** | Role checks in UI (`work.tsx:41,52,56`) mirrored by server `secretary_only` (`views.py:110-114`) **[verified]** |
| H13 | Production refuses to start in a weak configuration | **PASS** | `DJANGO_DEBUG` is now refused at import when `APP_ENV=production` (`settings.py:53-62`); previously only caught by the deploy-time `check --deploy` **[verified]** |
| H14 | Rate-limit credentials are never stored or logged in the clear | **PARTIAL** | `AXES_SENSITIVE_PARAMETERS` (`settings.py:124`) is narrower than the framework default (drops `password1`/`password2`/`old_password`); no current form is affected **[verified]** |
| H15 | Every stored table has a bounded lifetime | **FAIL** | No retention job, no cron in `render.yaml`; documented as an open item (`DEPLOYMENT.md:149`) **[verified]** |
| H16 | A failed migration cannot leave the service serving a broken schema | **PASS** | `migrate` is a `preDeployCommand` (`render.yaml:10`), so a failure blocks the release **[verified]** |
| H17 | A code rollback does not leave the database ahead of the code | **FAIL** | Documented as manual-only; no automated rollback, no migration down-path (`DEPLOYMENT.md`, `RENDER-DEPLOY.md`) **[verified]** |
| H18 | Backups exist and have been restore-tested | **PARTIAL** | Neon PITR described in prose; no verification in code or CI **[verified]** |
| H19 | An operator is paged when the service degrades | **FAIL** | Health check is `SELECT 1` only (`views.py:828-832`); no alerting or error tracking; JSON logs to stdout only **[verified]** |
| H20 | Dependencies are pinned and reproducible | **PASS** | `requirements.lock` + `pnpm-lock.yaml` with `--frozen-lockfile` in CI and Docker; Python 3.12 / Node 22 / pnpm 11.19.0 exact **[verified]** |

---

## 3. Verified findings, by severity

### High

**F1 — Google sign-in converts an existing password account with no owner consent.** `adapters.py:162-166`
resolves the user by `email__iexact`; `adapters.py:179-188` creates the `SocialAccount` link on first
contact. Consequences:
- Anyone who controls the victim's Google account gets the victim's secretary/auditor session.
- The account is then *permanently* Google-locked: `adapters.py:183-184` refuses a different
  identity afterwards, so the legitimate owner can only return via password reset.
- `google.account_linked` (`adapters.py:205`) is the only signal, and it is not delivered to the
  account owner.

Mitigating: Google's own account security is the trust anchor, and `email_verified` is checked
server-side (`adapters.py:87, 92-93`) against a token the browser cannot forge. This is a
**consent and notification** gap more than a bypass — but the irreversible lock-in is the part that
matters.

**Status: still FAIL, deliberately unfixed — decision required.** The remediation depends on a
product/security policy choice that is not derivable from the code, so it is documented rather than
guessed. Full analysis, the options considered, a recommendation, and the resulting test plan are in
[`DESIGN-F1-GOOGLE-LINKING.md`](DESIGN-F1-GOOGLE-LINKING.md). Recommendation: require an
already-authenticated session before linking when the account has a usable password, and allow
automatic linking only when it does not. Two related gaps are noted there as well: there is no way
to unlink or replace a Google identity (no disconnect route is mounted, and nothing deletes a
`SocialAccount`), which is what makes the lock-in permanent.

**Confirmed by simulation, not only by reading.** Driving the real callback with mocked Google
proved the whole chain end to end: a Google identity carrying an *existing* password user's verified
email was linked with no consent step at all, and the response was a fully valid session retaining
that user's existing **secretary** role. `SocialAccount(provider='google', uid='attacker-uid')` was
created and `google.account_linked` was written — the event exists, but the account owner never sees
it. The finding is a complete account takeover, not a partial one.

### Medium

**F2 — Security events the UI promises are never written.** **REMEDIATED 2026-09-30.**
`security.csrf.failure`, `security.suspicious_request`, and `system.startup` were declared in
`observability.py` and rendered as user-facing events by `audit_taxonomy.py`, but no code path
wrote any of them. Resolution, and it was deliberately not uniform:

- *`security.csrf.failure` — implemented, because a real producer exists.* Django routes every
  CSRF refusal through `settings.CSRF_FAILURE_VIEW`, so a wrapper at `middleware.py:110-150`
  records the event and then renders Django's own failure page unchanged. Attribution works: Django
  resolves the refusal after `AuthenticationMiddleware`, so a refusal against a signed-in account is
  filed under that account's organization, while an anonymous refusal gets a null organization and
  none is invented. Only Django's fixed reason string is stored — never the token, cookie or body.
  A regression guard (`test_observability.py`) asserts the wrapped middleware is still Django's own,
  because the first attempt subclassed `CsrfViewMiddleware` and silently turned on W003/W016.
- *`security.suspicious_request` — removed, because no detector exists.* A grep of the whole backend
  found no heuristic that could justify the name. Inventing one to make the catalogue look complete
  would have been worse than the gap.
- *`system.startup` — removed from the audit trail, recorded in the log instead.* A process start is
  not a tenant event, and an `AuditEvent` with no organization is returned by no tenant query, so a
  row would be invisible to every auditor while still growing the table. `serve.py` — the single
  production entry point, so no duplicate startup rows — now emits it via `technical()`.

**F3 — Disabling an account does not revoke its session.** **REMEDIATED 2026-09-30.**
`disable_account` cleared `is_active` and the access row but never deleted the `django_session` row.
While disabled, `membership()` correctly denied every request; on re-enable, the same cookie became
valid again without re-authentication. `access.py:19-83` adds `revoke_sessions()`, which matches on
the decoded `_auth_user_id` and deletes every row, so all of a user's sessions (one per browser) go
at once; `views.py:711-716` calls it and records the count in the audit event. Deliberately *not*
swallowed on failure: reporting "disabled" while leaving the old cookies working is the exact failure
the control exists to prevent.

**F4 — Financial integrity rests entirely on application code.** The only DB constraints on money
are positivity and per-(payment, month) uniqueness (`models.py:80, 90`). The
`SUM(allocations) == amount_received` invariant is checked by `check_finances.py:17`, which runs at
`preDeploy` and on demand. A code defect that breaks allocation symmetry can therefore sit in
production undetected between deploys.

**F5 — No retention anywhere.** `AuditEvent`, axes attempt tables, `RecoveryAttempt`, and
`django_session` grow without bound. For a ledger whose audit history is the primary evidence
surface, unbounded growth is both a cost and a privacy problem. `DEPLOYMENT.md:149` records this as
an open item, so it is known — but note the append-only ORM guards (`models.py:179-185`) mean any
future purge job must bypass them deliberately.

**F10 — A refusal's audit event is rolled back by the refusal itself.** **REMEDIATED 2026-10-01.**
Found by behavioural simulation, not by reading: `payment.replayed` and both `import.rejected`
outcomes were absent from the history after the request that should have written them had already
answered 400.

`api()` wraps writes in `transaction.atomic()`. The payment replay handler in `payments` wrote its
event from an `except PaymentRejected:` block *inside* that transaction, and its comment claimed
"the payment transaction has already rolled back by the time this runs". It had not — the caller is
still inside `api()`'s lock, and the `raise` on the next line is what ends it. The same shape
existed three times over: the expired import preview, the duplicate file (whose comment reasoned that
having no batch row meant "no transaction to roll it back with", which is not what `api()` does),
and the half-written import batch.

The fix is not to move the write, because any write inside the transaction dies with it. `defer_audit()`
(`services.py`) attaches the event to the exception, and `api()` writes it in every handler — including
one added for exceptions nothing anticipated, which is the case that lost `import.failed` most often.
The error still propagates unchanged, so a genuine fault keeps returning 500 rather than becoming a
tidy 400.

**F11 — A JSON endpoint answers a missing record with an HTML page.** **REMEDIATED 2026-10-01.**
`get_object_or_404` under `@api` raised `Http404`, which `api()` did not catch, so Django's own
handler rendered a full HTML error document for `/api/members/<pk>/`, `/api/accounts/<pk>/disable/`,
`/api/invites/<pk>/revoke/` and the rest. `api.ts:33` has no status-specific branch for a non-JSON
body, so a secretary following a stale link was told "The server could not complete this request"
rather than that the record is gone. `api()` now catches `Http404`, `Member.DoesNotExist` and
`Payment.DoesNotExist` and answers JSON 404 with one wording for every record type.

The status change is deliberate and load-bearing: a record in another organization now answers
*identically* to a record that does not exist. Previously `void` answered 400 for a foreign payment
while `member_detail` answered 404, so the status code alone confirmed which ids existed elsewhere.
`test_organizations.py` was updated to assert the stronger property.

**F12 — `int()` and ORM messages were handed to users.** **REMEDIATED 2026-10-01.**
`/api/payments/?page=abc` returned `invalid literal for int() with base 10: 'abc'`, and
`/api/payments/?page=99999999999999999999` overflowed the offset arithmetic into an `IntegrityError`
and a 409 about a "conflicting record". Payment pagination also had no cap, unlike the audit history,
so a huge page was an unbounded `OFFSET` — a cheap way to make the database walk a whole table in
order to discard the rows. `preview`, `pay` and `void` answered `Member matching query does not
exist.` and `Payment matching query does not exist.`; `import_preview` put the same text inside a
`Row 3: ...` message. Payments now reuse the audit history's bounded page reader
(`PAYMENT_PAGE_SIZE` / `PAYMENT_MAX_PAGE`), and the ORM wording is gone.

### Low

**F6 — `DJANGO_DEBUG` is a deploy-time, not boot-time, guard.** **REMEDIATED 2026-09-30.**
Previously only `check --deploy` (run as a deploy step, `deployment_check.py:41`) caught a production
process starting with DEBUG on; anything else booting production config — a shell, a one-off command —
would have served tracebacks. `settings.py:53-62` now refuses at import, matching the existing
`required()` fail-closed pattern used for `SECRET_KEY` and `ALLOWED_HOSTS`. Verified that the CI
build paths (`collectstatic`, `verify_static`, both `APP_ENV=production`) and the local/test modes are
unaffected, and that `check --deploy` remains clean.

**F7 — `AXES_SENSITIVE_PARAMETERS` narrows the framework default** (`settings.py:124`). No current
form is affected; a future form using `password1`/`old_password` would be stored unhashed.

**F8 — Receipts are derived, not issued artifacts.** `receipt_number` is a property over the row PK
(`models.py:75-77`); there is no issuance record, so a printed receipt cannot be invalidated or
traced to a print event. Voiding the payment is the only lever.

**F9 — `cleanup_pending_signups` is documented as daily but has no scheduler** (`render.yaml` has
no cron). The command exists; nothing runs it.

**H1 — The signup send cap could be reset by resubmitting the form.** **REMEDIATED 2026-10-01.**
This was the one claim in the audit that arrived unverified, so it was measured before being fixed.
`PendingSignup.code_sends` is a JSON list of send timestamps, and both the 60-second cooldown and
the 5-per-hour cap are measured against it (`signup.py:146-153`). But `create()` enforced "latest
submission wins" by **deleting** the existing row for that address, taking the counters with it
(`signup.py:111`). Measured on the pre-fix code: **8 POSTs to `/signup/` for one unregistered
address produced 8 verification emails**, the documented cap never engaging.

The fix overwrites the row in place and carries the send history forward, so the cap's memory
outlives the resubmission. Two things deliberately survive alongside it:

- **the invitation hash** — coming back through "wrong email" posts the form again without the token
  in the query string, so dropping it would strand an invited secretary;
- **the row count** — still one pending signup per address, so the uniqueness invariant holds.

The code itself is still voided on resubmission, because that is the point of asking for a new one,
and the cooldown now correctly applies to a resubmission. That last part changed a real user-visible
behaviour: "wrong email, go back, resubmit" no longer emails a fresh code immediately, and
`test_the_invitation_survives_going_back` was updated to reflect the cooldown rather than the fresh
send it used to get. Covered by `SignupSendCapTests` (8 tests), which also asserts the cap is
**per address** — one person's signup budget cannot be consumed by another's — and that a capped
address is refused with the same wording as any other, so the cap is not a probe for who is
registered.

Not fixed here: the Google login and callback routes still have no per-source throttle, so H1 stays
**PARTIAL** overall. Adding one needs care — a limit keyed on source address can lock out legitimate
users behind a shared NAT, which would turn a rate limit into an availability problem.

**H2 — Signup told the user a code was emailed when nothing was sent.** **REMEDIATED 2026-10-01.**
Found by simulating a provider outage. `send_code` called `send_mail(fail_silently=True)`, which
discards both the exception *and* the return value — the count of messages actually delivered. A 0
was indistinguishable from a success, so the verification page said "We emailed you a 6-digit code"
and then asked for a code that was never sent.

The second-order effect was worse than the wrong wording. By the time delivery was known to have
failed, the attempt had already been written to the row: a live code hash, and a timestamp appended
to `code_sends`. The cooldown was therefore spent on a mail that never left, so the visitor was told
to try again and their retry was refused by the cooldown — stuck on a page whose button does nothing.

Delivery is now checked rather than assumed, and on failure the code state is put back exactly as it
was, so a working provider on the next try is not locked out. `fail_silently=False` alone would not
have been enough: catching the exception still leaves the row already stamped. Covered by
`SignupDeliveryFailureTests`, which tests both failure shapes — a raised `OSError` and a provider that
returns 0 without raising — because only handling the first would have left the second reporting
success.

**H3 — A double click manufactured audit history nobody created.** **REMEDIATED 2026-10-01.**
Revoking an invitation twice wrote a second `invite.revoked` row and stamped `revoked_at` a second
time. `disable_account` and `enable_account` both guard against exactly this and answer
`already_disabled` / `already_enabled`; `revoke_invite` did not. The visible symptom is small, but it
is on the surface the product exists to trust: an auditor reading the trail would see an invitation
cancelled by two separate decisions when only one was ever made. Now idempotent, answering
`already_revoked`, and `test_error_contract.py` asserts one event for two clicks.

---

## 4. Retracted claims

Recorded so they are not re-investigated. Both came from delegated research and failed direct
verification.

- **"An unbounded `billing_end` triggers a 1200-month loop."** Not supported. `services.py:149`
  breaks the loop at `billing_end` and `services.py:160` raises on an over-long span.
- **"A client-side payment timeout can cause a duplicate payment."** Not supported. The form
  disables on submit, and the idempotency key makes a retry safe by construction (H2).

---

## 5. Adversarial scenarios

1. **Attacker controls the victim's Google account, then releases it.** They hold a secretary
   session and a permanently Google-locked account. (F1)
2. **CSRF flood against a logged-in secretary.** Every request is rejected by the middleware; the
   refusal is now recorded against the secretary's own organization, so it is visible in their audit
   history. Note the residual limit: an unauthenticated flood has no organization and therefore no
   tenant-visible destination, so it lands only in the technical log. (F2 partial, H1)
3. **Stolen session cookie, account disabled then re-enabled.** *Closed.* The cookie is now destroyed
   at disable time and stays invalid across re-enable; a fresh sign-in is required. (F3)
4. **Allocation defect ships in a release.** `check_finances` runs pre-deploy against the
   pre-release code path, so the check can pass while the defect is live; balances are then wrong
   until the next deploy or manual run. (F4)
5. **Unbounded audit growth.** Table growth degrades the auditor query that is the product's core
   evidence surface. (F5)
6. **Scripted signup-form loop against an unregistered victim address.** *Closed.* Each POST used to
   delete the pending signup and with it the send history, so the 5/hour cap could be reset to zero
   per request; measured 8 emails from 8 POSTs. The history now survives resubmission, and both the
   cooldown and the hourly cap hold. (H1)

---

## 6. Test coverage classification

| Area | Automated | Semi-automated | Manual |
|---|---|---|---|
| Tenant isolation (H4) | Yes | — | — |
| Payment idempotency (H2) | Yes | — | — |
| Production config (H13) | Yes | — | — |
| Migration drift | Yes | — | — |
| Allocation symmetry (H3) | `check_finances` only | — | Yes |
| Session revocation on disable (H5) | **Yes** — `test_disable_account` | — | — |
| Google account-linking (H6) | Partial | — | Yes |
| Audit completeness (H7) | **Yes** — `test_observability` | — | — |
| Signup send cap (H1) | **Yes** — `SignupSendCapTests` | — | — |
| Production DEBUG guard (H13) | **Yes** — `test_production_config` | — | — |
| Retention (H15) | No | — | — |
| Backup restore (H18) | No | — | Yes |
| Incident alerting (H19) | No | — | Yes |

The two gaps called out as worth converting to regression tests first — F3 and F2 — are now both
covered, and the previously untested signup send cap is covered too. What remains untested above is
untested for a reason: it needs either a decision (H6) or infrastructure that does not exist in the
repo (H18, H19).

---

## 7. Remediation roadmap

**Now — small, high-confidence, no design change** — **all three done**
1. F2 — **REMEDIATED.** `security.csrf.failure` now has a real producer via `CSRF_FAILURE_VIEW`;
   `security.suspicious_request` and `system.startup` were deleted from the allowlist and taxonomy
   because no detector or lifecycle event produces them. Startup is a technical log instead.
2. F3 — **REMEDIATED.** `disable_account` deletes every `django_session` row for the user
   (`access.py:revoke_sessions`) and records the count in the audit event.
3. F6 — **REMEDIATED.** `APP_ENV=production` with a truthy `DJANGO_DEBUG` now raises
   `ImproperlyConfigured` at import rather than only being caught by `check --deploy`.

**Next — needs a decision from you**
4. F1: see [`DESIGN-F1-GOOGLE-LINKING.md`](DESIGN-F1-GOOGLE-LINKING.md). The analysis, six options, a
   recommendation (require an authenticated session to link when a usable password exists; allow
   automatic linking when it does not), and the test plan are written up. **Not implemented**, because
   whether Google-only accounts may be auto-linked is a product decision. Also undecided: there is no
   way to unlink or replace a Google identity, which is what makes the lock-out permanent.
5. H1: **the delegated cap-bypass finding was confirmed empirically and is now fixed** — 8 signup
   POSTs against one unregistered address produced 8 emails despite a documented 5/hour cap, because
   `create()` deleted the row holding `code_sends` (`signup.py:107-138`). Covered by
   `SignupSendCapTests`. **Still open:** the Google login and callback routes have no per-source
   throttle, and Axes is wired only to the password form. A source-based limit needs care so it
   cannot be used to lock out legitimate users behind a shared NAT.
6. F9: add a cron job to `render.yaml` for `cleanup_pending_signups` and a `clearsessions` run.
7. F5: write a retention policy and a purge command that deliberately bypasses the append-only
   guards; assert the documented `REVOKE UPDATE, DELETE` in `deployment_check` so the blueprint
   matches the documented posture.

**Later — deliberate, larger**
7. F4: decide whether allocation symmetry needs a stronger guarantee than a management command.
8. H19: add error tracking and an alert on `/health/` and on error-rate in logs.
9. CSP `style-src 'unsafe-inline'` is load-bearing (`work.tsx` inline styles, `theme.js`/`signup.js`
   `setProperty`, Django-injected branding CSS). Removing it is a coordinated refactor, not a
   header change.

---

## 8. Operational readiness

- **Deploy:** `migrate` + `deployment_check` pre-deploy, `verify_static` in build, `/health/` on the
  service. A failed migration blocks the release rather than half-deploying it (H16).
- **Rollback:** manual. Code rollback does not roll back the schema; recovery means restoring the
  database (H17). This is the largest untested operational assumption in the system.
- **Backups:** Neon PITR is described but never verified in code or CI (H18).
- **Monitoring:** structured JSON logs with end-to-end `request_id` correlation
  (`observability.py:140-154`, `middleware.py:65`) and audit rows carrying the same id
  (`services.py:70`). No alerting, no error tracking, no dashboards (H19). A 500-path response does
  not receive the `X-Request-Id` header because the exception is re-raised first
  (`middleware.py:62`) — the id is still in the log line.

---

## 9. What is genuinely strong

Worth stating plainly, because most audits only list defects:

- **Tenant isolation is deliberate and well-reasoned**, including 404-instead-of-403 to prevent
  cross-tenant existence probing, and re-filtering name lookups per tenant.
- **Payment idempotency is correctly designed** — unique key, fingerprint comparison, replay
  returns the original, conflicting reuse is rejected and audited. This is the single best control
  in the codebase.
- **Actor and IP provenance is sound** — the decision *not* to read `X-Forwarded-For` is correct and
  documented, and the UI discloses when a recorded IP is the proxy's rather than the member's.
- **Secret redaction is defence-in-depth done right** — explicit fields from callers, recursive
  name-based redaction, size caps.
- **Supply chain is reproducible** — locked deps, frozen lockfiles, real-Postgres CI job, Docker
  build in CI.

---

## 10. Confidence and limits of this audit

- Every **[verified]** claim was read in source during this pass.
- The one **[unverified]** claim (that signup per-address caps could be reset by re-POSTing the
  form) has since been **confirmed empirically** and fixed. What was measured, before the fix:
  **8 verification emails from 8 POSTs against one unregistered address**, despite a documented
  5/hour cap and a 60-second cooldown. There are no remaining unverified claims in the status table.
- Not covered in depth: concurrent-import behaviour at volume, spreadsheet formula-injection in
  exports, and the accessibility of the sidebar/modal focus management.
- The audit itself read code and ran no tests; `209 OK (skipped=2)` was a pre-audit baseline.
  Subsequent remediation passes ran the suite (see §11).

---

## 11. Remediation verification log

Commands below were run from `backend/` with `TEST_SQLITE=1` unless stated. Anything not run is not
claimed.

### Backend

Re-verified after the behavioural-simulation round (F10, F11, F12, H2, H3), on 2026-10-01:

| Command | Result |
|---|---|
| `manage.py test --noinput` (full suite) | **361 tests, OK, skipped=2** |
| `manage.py test ledger.test_error_contract` (new) | **14 tests, OK** |
| `manage.py test google_auth.tests.SignupDeliveryFailureTests` (new) | **6 tests, OK** |
| `manage.py makemigrations --check --dry-run` | **No changes detected** |

The suite is now fully green. The long-standing
`test_member_report_uses_the_requested_month` failure (`AssertionError: 50 != 25`) turned out to be a
bug in the test rather than the report, and was fixed: it asserted literal arrears figures of 25 and
125 while silently assuming the member's joining month and the month the test ran in were the same
one. `setUp` joins the member in September 2026, so from October 2026 onwards the report correctly
answered 50 for a member who owes for two months. The expected figures are now derived from the
member's own joining month, and the post-payment figure subtracts the allocations read back out of
the workbook instead of assuming zero.

Findings were reproduced by driving the real HTTP surface with isolated per-test data, not by reading
code: every new regression test in `test_error_contract.py` and `SignupDeliveryFailureTests` failed
against the pre-fix behaviour.

### Deployment checks

| Command | Result |
|---|---|
| `manage.py check --deploy` (`APP_ENV=production`) | **System check identified no issues (0 silenced)** |
| `manage.py collectstatic --noinput` (`APP_ENV=production`) | Succeeded — 12 files copied, 36 post-processed |
| `manage.py verify_static` (`APP_ENV=production`) | **Verified 10 template/static assets** |
| `manage.py deployment_check` | **Cannot run locally** — no reachable Postgres (`password authentication failed`). Needs a real database. |

### Frontend

| Command | Result |
|---|---|
| `npm run build` (`tsc --noEmit && vite build`) | **Passed** — built in 5.15s |
| `npm test` | **79 tests: 79 pass, 0 fail** |

The two previously failing frontend assertions
(`each auth submit button carries a busy label`, `the signup theme preview themes the whole page,
not only the box`) now pass against the untracked WIP they were written for. `api.ts` needed no
change for F11: with a JSON 404 the existing `result.error` branch now reports the server's wording,
where the HTML page had fallen into the catch-all branch.

### Deliberately not run

- No `X-Forwarded-For` trust was introduced, so no proxy-header configuration was exercised.
- No rate-limit configuration was changed, so no tuning or load test applies.
- Live Google OAuth was never exercised; all Google tests use `mocked_google`.
- **Concurrency is unverified.** PostgreSQL 18 is running locally but every attempted credential
  failed authentication, and SQLite answered concurrent writes with `database table is locked`. No
  claim is made about behaviour under concurrent writers; F4 remains open and is the right place to
  look.
