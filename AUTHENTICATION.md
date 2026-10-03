# Authentication

How a person gets into Duesdesk, and what the server records when they cannot.

There is no external identity provider. A secretary account and its organization are
created immediately, in one transaction, and the new secretary is signed in on the
spot. Signing up does not prove control of the email address; password recovery is the
mechanism that proves it later. Everything else is a Django password behind
`django-axes` throttling.

## The signup flow

Submit the secretary signup form with a username, an email address, a password and an
organization name. The account, the organization, the secretary membership and the
`EmailClaim` that reserves the address are all written in a single transaction; the
user is signed in only after that transaction commits. Nothing is sent by email, and
no account is left half-created if the transaction fails.

The handler then sends the new secretary to `/signup/branding/`, the first-run
branding step. That page offers a workspace name, a logo and three colours, and can be
skipped. Submitting it writes the branding through the same settings path the rest of
the application uses and marks onboarding complete; skipping marks onboarding complete
without changing the palette. Both the account step and the branding step can be
completed without JavaScript.

If the form is submitted with an invitation token, the new account joins the inviting
organization instead of creating one, and inherits its branding, so the branding step
is skipped.

Repeating a signup with an address that already has an account is refused with a
generic message, before anything is written. The check examines `EmailClaim` first and
then existing users case-insensitively, so an address one person used in any casing
cannot be reused. Two requests that race still cannot both win: the database-level
uniqueness of `EmailClaim` is the final authority, and the loser gets the same refusal
message as if it had arrived second.

## Security and storage

- Creating the account immediately means signup does **not** prove the address belongs
  to the person who typed it. An attacker can register an address they do not control,
  but they cannot read its mail and cannot reset a password they do not already know,
  so the address is not usable by the real owner until they recover it. See
  `docs/DESIGN-SIGNUP-VERIFICATION.md` for the deliberate decision and the non-blocking
  verification design that follows.
- Signup attempts are rate limited per client IP and per lowercased email address. A
  limited request gets a `429`, the same generic page, and is not recorded, so the
  limiter is not a probe for which addresses exist. The limiter is a courtesy against
  bulk abuse only; concurrent signup relies on database uniqueness, never on the
  limiter, to decide the winner.
- Passwords are hashed by Django's configured hashers and never logged.
- A refusal from `SignupForm.clean_email` and a lost race produce the same message, so
  the response does not reveal whether an address is already registered.
- Known-user events use `ledger.AuditEvent` unchanged. Pre-account refusals use
  `google_auth.AuthRejection`, which has no organization column: see the appendix.
- Existing invitations retain their email checks, expiry, organization and
  single-use rules. An invitation for a disabled organization is refused.
- The `EmailClaim` row is what reserves an address at the database level. A claim is
  released only by an operator, never by a failed signup, so an address cannot be
  cycled between accounts by rapid attempts.

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
2. Keep the existing frontend build and collectstatic commands. The branding step's
   theme and preview scripts are static assets and must be collected with the release.
3. Run `python backend/manage.py migrate --noinput` from the repository root, or
   `python manage.py migrate --noinput` with backend as Root Directory. This applies
   the pending `google_auth` and `ledger` migrations: `EmailClaim` and `SignupAttempt`
   are created and `PendingSignup` is removed; `Organisation` gains
   `onboarding_required`, `onboarded_at` and `disabled_at`. The `EmailClaim` backfill
   reads every existing address; if two accounts use an address that differs only in
   case it **aborts**, naming the conflicting addresses, rather than silently picking
   one. Resolve the reported conflicts and re-run the migration.
4. Configure and test email delivery; password recovery still sends mail.
5. Test signup with a new address, the branding step (save and skip), password login,
   invitation signup, access isolation, and password recovery for one account that
   previously had no password. Use test organizations deliberately; no production
   record needs to be cleared.
6. Stale-organization sweep, from the repository root, is **manual** (see below):
   `python backend/manage.py disable_stale_organizations --dry-run`
   `python backend/manage.py disable_stale_organizations`

Organizations abandoned during onboarding (signed up, never branded, never added a
member or a payment) can be disabled with `disable_stale_organizations`. It defaults to
30 days, always prints what it would disable, and re-checks each organization under a
row lock before writing, so it is safe to run by hand repeatedly. It is not a cron and
no schedule is configured: run it from the existing service's Shell or an
already-authorized scheduler when an operator decides to. Disabling is reversible by
clearing `disabled_at`; a disabled organization's secretary can still recover their
account through password reset.

## Validation

From `backend`, with `TEST_SQLITE=1` and `APP_ENV=local`:
`python manage.py test --noinput`
`python manage.py makemigrations --check --dry-run`

Tests never contact a mail transport or an identity provider. SQLite verifies rollback
and stale-object handling; the signup concurrency tests are skipped on SQLite and must
pass CI's PostgreSQL run before treating concurrent signup as verified on Neon. The
`EmailClaim` uniqueness constraint is what makes those tests meaningful.

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
account that resolves to a membership: `organization.created`, `organization.joined`,
`auth.login.success`, `auth.password.changed`, `auth.password_reset.completed`, and any
refusal that names a real account. Same `organization`, same `actor`, same `details`
JSON shape as every other ledger event. A refusal is written with outcome `rejected`
and the short reason. A refused sign-in for a known account is exactly the security
event that account's organization can and should see.

**`google_auth.AuthRejection` — the tenantless record.** Used for every attempt that
could not be attributed to an account: an unknown address, an address matching no user
or two users, an account with no membership, a refused sign-in, and a rejected signup
whose address could not be attributed. Fields: `action`, `reason`, `flow`, `method`,
`email`, `user` (nullable), `ip`, `created_at`.

A refusal that does resolve to a real account is written **to both tables**: the
tenantless row here, and a tenant-visible `AuditEvent` under that account's
organization. A refusal that resolves to no account exists only here, because there is
no organization to file it under and inventing one would be a lie.

This table deliberately has **no organization column and no organization foreign
key**, and no application view reads it. `user` is `SET_NULL`, so deleting an account
does not erase the record. Nothing is filed under an organization that did not ask for
it.

`flow` says which entry point the browser had started, so a refused invitation signup
is never confused with a refused sign-in. `method` is retained for historical rows;
signup no longer proves an address, so no new row writes a verification method.

The table stores the address that was presented, with its original casing. For an
unknown address that belongs to a person who has no account here, so treat the table
as confidential operational data. `org:admin` permissions still apply; the table is
not exposed in the UI.