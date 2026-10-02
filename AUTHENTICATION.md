# Authentication

How a person gets into Duesdesk, and what the server records when they cannot.

There is no external identity provider. Signup verification and password recovery both
work by emailing a code, and everything else is a Django password behind
`django-axes` throttling.

## The signup flow

Submit the secretary signup form. A `PendingSignup` is stored with a lowercased
email, username, organization name, password hash, branding, invitation hash, random
session token and a 24-hour expiry. No account, organization or membership exists
yet. A six-digit code is emailed; delivery failure is reported honestly and no code
is left behind that nobody received.

The verification page offers that code, resend with a countdown, and a back action
that retains everything typed except the password.

The correct code permits one atomic completion: organization (or the invitation's
organization), user, secretary membership, audit event, consumption of the
invitation, and deletion of the pending row. The user is signed in after the commit.
Nothing can be created without a validated pending signup in the session.

## Security and storage

- Pending handles live in the session, never in an email or a numeric id in a URL.
  No query parameter, form field or header can name a pending signup, which is what
  makes it impossible to complete somebody else's.
- Passwords and codes are hashed, and the code hash records the algorithm that made
  it. Django answers `False` for a hash made by an algorithm that is no longer
  configured, so the hasher is not changed between sending and verifying a code.
- Six-digit codes expire after 10 minutes, and five wrong attempts invalidate one.
  Sends are limited to one per 60 seconds and five per sliding hour. The count lives
  on the pending row, and a resubmission updates that row rather than replacing it,
  so a script cannot reset its own budget. Row locks serialize sends and attempts,
  including requests that arrive holding a stale model object.
- `verified_at` records successful code proof. Completion rechecks proof, expiry,
  session ownership and email uniqueness. A failed account creation leaves no
  partial user, organization or membership; request a new code after one.
- Console and file email backends are refused for codes, so a code is never printed
  or written to disk as plaintext. Transport exceptions are logged without message
  bodies.
- Known-user events use `ledger.AuditEvent` unchanged. Pre-account refusals use
  `google_auth.AuthRejection`, which has no organization column: see the appendix.
- Existing invitations retain their email checks, expiry, organization and
  single-use rules.

## Accounts created by the removed provider

Duesdesk used to accept an external identity instead of a password. Those accounts
have no usable password, because nothing ever set one, so when the provider was
removed they would have been locked out of their own organizations: Django refuses
to send a reset link to exactly those accounts.

Migration `google_auth.0006_record_legacy_identities` reads the provider's identity
table directly and writes one `LegacyIdentity` row per affected account. It is
deliberately narrow:

- only active accounts with no usable password are marked, so an account that
  already has a password gains nothing and keeps Django's normal rules;
- a row whose provider payload cannot be parsed is still marked, because skipping it
  would lock that person out;
- the provider's own tables are left exactly as they are, forwards and backwards.

`AccountRecoveryForm` offers a reset link to an account with a usable password, and
additionally to one carrying a `LegacyIdentity` row. `AuditablePasswordResetConfirmView`
deletes the row as soon as the password is set, so the grant is spent once and the
account is an ordinary password account from then on. A disabled account is never
marked and never granted a link.

A fresh database never had the provider's table; the migration detects that and
does nothing. `google_auth/test_migrations.py` proves both cases by driving the real
migration executor.

## Email configuration

Set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
`EMAIL_USE_TLS` and `DEFAULT_FROM_EMAIL` in secret environment settings.
`EMAIL_BACKEND` defaults to Django's SMTP backend. Never commit credentials.

Render Free blocks outbound SMTP ports 25, 465 and 587:
https://render.com/docs/free An email API backend or an SMTP-capable paid instance is
needed for delivery there. Choosing and authorizing that provider or plan remains an
operator decision; no mail provider, subscription or service is created by this
repository. Configured settings do not prove delivery works: verify a real inbox
before relying on it.

Optional HTTPS delivery uses Resend's email API. Set `RESEND_API_KEY` as a secret and
`DEFAULT_FROM_EMAIL` to an address on your verified sending domain; the backend is
selected automatically when the key is present, unless `EMAIL_BACKEND` overrides it.

## Render release steps

1. Publish this code to the existing Duesdesk service repository.
2. Keep the existing frontend build and collectstatic commands. The verification
   countdown is a static asset and must be collected with the release.
3. Run `python backend/manage.py migrate --noinput` from the repository root, or
   `python manage.py migrate --noinput` with backend as Root Directory. This applies
   the pending `google_auth` migrations, including the provider removal.
4. Configure and test email delivery.
5. Test signup with a new address, password login, invitation signup, access
   isolation, and password recovery for one account that previously had no password.
   Use test organizations deliberately; no production record needs to be cleared.
6. Cleanup command from the repository root:
   `python backend/manage.py cleanup_pending_signups --dry-run`
   `python backend/manage.py cleanup_pending_signups`

Render cron configuration, if separately authorized:
- Repository: kinkaed/Duesdesk, branch main; Root Directory: backend.
- Build: `pip install -r requirements.lock`.
- Command: `python manage.py cleanup_pending_signups`.
- Schedule: `0 * * * *` (hourly, UTC).
- Use the same `DATABASE_URL`, `APP_ENV`, `DJANGO_SECRET_KEY` and relevant Django
  settings as the existing service, entered as secrets.

Render Cron Jobs are a separate service type: https://render.com/docs/cronjobs
No cron service has been created. Under that restriction run the cleanup command in
the existing service's Shell or an already-authorized scheduler. Expired pending
signups cannot authenticate even when physical cleanup has not yet run.

## Validation

From `backend`, with `TEST_SQLITE=1` and `APP_ENV=local`:
`python manage.py test --noinput`
`python manage.py makemigrations --check --dry-run`

Tests never contact a mail transport or an identity provider: the emailed code is
read from the in-memory backend, so delivery and code paths are exercised without
external dependencies. SQLite verifies rollback and stale-object handling;
PostgreSQL row-lock concurrency must also pass CI's PostgreSQL tests before treating
concurrency as verified on Neon.

## Appendix: audit trail, addresses and lockout

Behaviour this repository guarantees that is not described by the flow above.

### IP addresses

Audit records use `request.META['REMOTE_ADDR']` only, validated as an IP address and
stored as text (empty when malformed). Forwarded headers are never read:
`X-Forwarded-For`, `X-Real-IP` and `CF-Connecting-IP` are attacker-supplied unless a
trusted proxy rewrites them. A test asserts that a spoofed forwarded header does not
appear in the record.

### Password lockout

`django-axes` owns password lockout. `GuardedAuthenticationForm` checks
`AxesProxyHandler.is_allowed` before Django's own authentication, so the limit and
the locked screen are unchanged even though `ModelBackend` now runs first.

### Audit trail

Two tables, chosen so that neither is made to lie about who owns an event.

Nothing writes to `ledger.AuditEvent` directly. Every authentication event goes
through `ledger.services.audit`, the same helper the rest of the application uses,
so a row carries the same correlation the technical log does: request id, method,
path, client address and user agent, plus redaction and a size cap on `details`.

**`ledger.AuditEvent` — the tenant-visible history.** Used for every event about an
account that resolves to a membership: `signup.email_verified`, `auth.login.success`,
`auth.password.changed`, `auth.password_reset.completed`, and any refusal that names
a real account. Same `organization`, same `actor`, same `details` JSON shape as
every other ledger event. A refusal is written with outcome `rejected` and the short
reason. A refused sign-in for a known account is exactly the security event that
account's organization can and should see.

**`google_auth.AuthRejection` — the tenantless record.** Used for every attempt that
could not be attributed to an account: an unknown address, an address matching no
user or two users, an account with no membership, a refused verification, and a
request with no flow in the session. Fields: `action`, `reason`, `flow`, `method`,
`email`, `user` (nullable), `ip`, `created_at`.

A refusal that does resolve to a real account is written **to both tables**: the
tenantless row here, and a tenant-visible `AuditEvent` under that account's
organization. A refusal that resolves to no account exists only here, because there is
no organization to file it under and inventing one would be a lie.

This table deliberately has **no organization column and no organization foreign
key**, and no application view reads it. `user` is `SET_NULL`, so deleting an account
does not erase the record. Nothing is filed under an organization that did not ask for
it.

`flow` says which entry point the attempt came from, so a refused verification is
never confused with a refused sign-in. `method` says how an address was being proven,
`code`.

The table stores the address that was presented, with its original casing. For an
unknown address that belongs to a person who has no account here, so treat the table
as confidential operational data. `org:admin` permissions still apply; the table is
not exposed in the UI.