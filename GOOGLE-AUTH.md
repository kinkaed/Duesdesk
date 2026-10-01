# Email verification and Google authentication

## Implemented flow

Submit the existing secretary signup form. A PendingSignup is stored with a
lowercased email, username, organization name, password hash, branding, invitation
hash, random session token, and a 24-hour expiry. No account, organization or
membership exists yet. A first email code is attempted; delivery failure is shown
honestly and Google remains available. The verification page offers Google,
a six-digit code, resend with a countdown, and a back action retaining values
except the password.

A matching verified Google address or the correct emailed code permits one atomic
completion: organization (or the invitation's organization), user, secretary
membership, verified EmailAddress, audit, optional Google identity link, invitation
consumption, and deletion of the pending row. The user is logged in after commit.
Google cannot create anything without an existing validated pending signup.

Existing password login is unchanged. Google login opens only one existing active
secretary/auditor account with a matching verified email. It never completes signup.
Google-only users can use the existing password-reset flow.

## Security and storage

- One callback: `/accounts/google/login/callback/`.
- Separate entry points: GET `/accounts/google/login/` and POST
  `/accounts/google/verify/` (CSRF-protected).
- Allauth stores an OAuth state containing the flow, pending handle and random
  attempt nonce server-side. A callback is rejected before token exchange if a
  newer signup/login has replaced its flow, even within the same browser.
- Pending handles are kept in the session, never emails or numeric IDs in URLs.
- Passwords and codes are hashed; code comparison uses Django's password checker.
- Six-digit codes expire after 10 minutes and five wrong attempts invalidate them.
  Sends are limited to one per 60 seconds and five per sliding hour. Row locks
  serialize sends and attempts, including requests using stale model objects.
- `verified_at` records successful code proof. Completion rechecks proof, expiry,
  session ownership and email uniqueness. Failed account creation leaves no partial
  user, organization or membership. Request a new code after a failed completion.
- Console/file email backends are refused for codes, so codes are never printed or
  written as plaintext files. Transport exceptions are not logged with message bodies.
- `SOCIALACCOUNT_STORE_TOKENS=False`; scopes are openid, email, profile, access online.
- Known-user events use ledger.AuditEvent unchanged; pre-account rejections use
  GoogleAuthRejection with flow, method, specific reason, time and IP.
- Signup Google links produce both signup_email_verified and google_account_linked.
- Existing invitations retain email checks, expiry, organization and single-use rules.
- The new migration only adds nullable verified_at to google_auth.PendingSignup;
  it does not change any financial or audit table.

## Email configuration

Set EMAIL_BACKEND (defaults to django.core.mail.backends.smtp.EmailBackend),
EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_TLS and
DEFAULT_FROM_EMAIL in secret environment settings. Never commit credentials.
Local tests use the in-memory backend and fake Google replies, not real mail.

Render Free blocks outbound SMTP ports 25, 465 and 587:
https://render.com/docs/free
An email API backend or an SMTP-capable paid instance is needed for delivery there.
Choosing/authorizing that provider or paid plan remains an operator decision.
No new mail provider, subscription or service is created by this change.
SMTP settings alone do not establish that mail arrives: verify an actual recipient
inbox after configuring delivery. Until then, Google is the available production
verification option and code delivery displays a recoverable error.

## Google Console

Keep the existing Web application client. No second callback is needed.
Authorized redirect URIs:
- https://duesdesk.onrender.com/accounts/google/login/callback/
- http://localhost:10000/accounts/google/login/callback/
- http://localhost:8000/accounts/google/login/callback/

GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET remain environment-only. Configure the
consent screen and authorized test users if the Google app is in Testing mode.

## Render release steps

1. Publish this code to the existing Duesdesk service repository.
2. Keep the existing frontend build and collectstatic commands. The new verification
   countdown is a static asset and must be collected with the release.
3. Run `python backend/manage.py migrate --noinput` from the repository root.
   With backend as Root Directory, use `python manage.py migrate --noinput`.
   This applies any pending google_auth migrations, including 0004.
4. Configure and test email delivery; Google credentials use the existing callback.
5. Test signup with a new address by both methods, existing-account Google/password
   login, invitation signup, and access isolation. Use test organizations deliberately;
   no production records were cleared as part of development.
6. Cleanup command from the repository root:
   `python backend/manage.py cleanup_pending_signups --dry-run`
   `python backend/manage.py cleanup_pending_signups`

Render cron configuration, if separately authorized:
- Repository: kinkaed/Duesdesk, branch main; Root Directory: backend.
- Build: `pip install -r requirements.lock`.
- Command: `python manage.py cleanup_pending_signups`.
- Schedule: `0 * * * *` (hourly, UTC).
- Use the same DATABASE_URL, APP_ENV, DJANGO_SECRET_KEY and relevant Django settings
  as the existing service, entered as secrets.

Render Cron Jobs are a separate service type: https://render.com/docs/cronjobs
The task also prohibits a new Render service, so no cron service has been created.
Under that restriction run the cleanup command in the existing service's Shell or
an already-authorized scheduler. Expired pending signups cannot authenticate even
when physical cleanup has not yet run.

## Validation

From backend, with TEST_SQLITE=1 and APP_ENV=local:
`python manage.py test --noinput`
`python manage.py makemigrations --check --dry-run`

OAuth tests mock Google's network replies; they test both real Django callback paths
but do not prove live Google consent or real email delivery. SQLite verifies rollback
and stale-object handling; PostgreSQL row-lock concurrency must also pass CI's
PostgreSQL tests before treating concurrency as verified on Neon.

## Completion update

The existing Waitress process now cleans up expired pending signups at startup and
hourly while awake, in batches of 500. No extra Render service or paid scheduler is
needed. Render Free sleeping suspends this work; cleanup resumes on startup. Expiry
is still enforced during every verification regardless of cleanup timing. The manual
management command remains available for larger backlogs.

Optional HTTPS delivery is implemented using Resend's email API. Set RESEND_API_KEY
as a secret and DEFAULT_FROM_EMAIL to an address on your verified sending domain.
The backend is selected automatically when the key is present, unless EMAIL_BACKEND
explicitly overrides it. It supports verification codes and password-reset mail.
No Resend account, paid plan or domain has been purchased/created by this release.
The sending account/key and verified sender must be supplied by the operator before
real email delivery can be verified. SMTP remains available as an alternative.

## Appendix: audit trail, addresses and lockout

Behaviour this repository guarantees that is not described by the flow above.

### IP addresses

Audit records use `request.META['REMOTE_ADDR']` only, validated as an IP address and
stored as text (empty when the value is malformed). Forwarded headers are never read:
`X-Forwarded-For`, `X-Real-IP` and `CF-Connecting-IP` are attacker-supplied unless a
trusted proxy rewrites them. A test asserts that a spoofed forwarded header does not
appear in the record.

### Password lockout

`django-axes` still owns password lockout. `GuardedAuthenticationForm` checks
`AxesProxyHandler.is_allowed` before Django's own authentication, so the limit and the
locked screen are unchanged even though `ModelBackend` now runs first.

A successful **Google** sign-in is a different credential from the password, so it is not
refused by a password lockout and it clears that lockout (`AXES_RESET_ON_SUCCESS = True`).
A test records this behaviour deliberately.

### Audit trail

Two tables, chosen so that neither is made to lie about who owns an event.

Nothing writes to `ledger.AuditEvent` directly. Every Google event goes through
`ledger.services.audit`, the same helper the rest of the application uses, so a Google
row carries the same correlation the technical log does: request id, method, path,
client address and user agent, plus redaction and a size cap on `details`.

**`ledger.AuditEvent` — the tenant-visible history.** Used for every event about an
account that resolves to a membership: `signup.email_verified`, `google.login_success`,
`google.account_linked`, and any refusal that names a real account. Same `organization`,
same `actor`, same `details` JSON shape as every other ledger event. The Google events add
a `method` key (`google` or `code`); a refusal is written with outcome `rejected` and the
short reason. A refused sign-in for a known account is exactly the security event that
account's organization can and should see.

**`google_auth.GoogleAuthRejection` — the tenantless record.** Used for every attempt
that could not be attributed to an account: an unknown address, an unverified claim, an
address matching no user or two users, an account with no membership, an OAuth error or
cancellation, a provider with no credentials configured, a refused verification, and a
callback with no flow in the session. Fields: `provider`, `action`, `reason`, `flow`,
`method`, `email`, `user` (nullable), `ip`, `created_at`.

A refusal that does resolve to a real account is written **to both tables**: the
tenantless row here, and a tenant-visible `AuditEvent` under that account's organization.
A refusal that resolves to no account exists only here, because there is no organization
to file it under and inventing one would be a lie.

This table deliberately has **no organization column and no organization foreign key**,
and no application view reads it. `user` is `SET_NULL`, so deleting an account does not
erase the record. Nothing is filed under an organization that did not ask for it.

`flow` says which entry point the attempt came from, so a refusal to verify a signup
address is never confused with a refusal to sign in. `method` says how the address was
being proven, `google` or `code`.

Note that the table stores the address Google presented, with the provider's own casing.
For an unknown sign-in that address belongs to a person who has no account here, so treat
the table as confidential operational data. `org:admin` permissions still apply; the table
is not exposed in the UI.
