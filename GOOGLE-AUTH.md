# Google authentication (backend only)

Duesdesk uses Google for two separate things, and the two are deliberately kept apart:

| Flow | What Google is asked | What it may create |
| --- | --- | --- |
| **Verify a signup** | Prove you own the address you just registered | Nothing at all. It finishes a signup that a code or Google has already started. |
| **Sign in** | Open an account that already exists | Nothing at all. |

Google can never create a user, a membership, an organization or an invitation on its
own. It is one of two ways to prove an address; the other is a six-digit code emailed to
the address itself. Whichever proves it, the account and the organization are created by
`google_auth/signup.py:complete()` — one place, one transaction, all or nothing.

## What exists today

| Item | State |
| --- | --- |
| Code and tests | Written and passing locally (153 tests, 2 skipped) |
| `google_auth.0001_initial` migration | Applied to Neon on 2026-09-30 |
| `google_auth.0002_googleauthrejection_flow` migration | **Written, not yet applied to Neon** |
| `google_auth.0003_pending_signup` migration | **Written, not yet applied to Neon** |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Configured in Render, ignored by local `backend/.env` |
| Google OAuth client in Google Cloud | Created in project model-gearing-510212-u9; Testing audience |
| This release | **Not deployed to Render** |

Until the two environment variables are set, the Google URLs refuse with the ordinary
page and an explanatory message, and nothing else changes. Password login, the emailed
code, secretary signup and every existing account keep working exactly as before.

## The three URLs

```text
GET  /accounts/google/login/            start sign-in
POST /accounts/google/verify/           start verification, from the verification page
GET  /accounts/google/login/callback/   Google returns you here — the only redirect URI
```

`/signup/verify/` is the verification page itself, served by this application, not by
allauth. It offers **Continue with Google**, the code box, and **Email me a code**.

Which flow the callback is serving is decided by the session and by nothing else. No
query parameter, form field or header can select a flow, so a callback aimed at the wrong
job simply refuses. Because there is one callback, **one** redirect URI is registered in
Google Cloud (see below).

No allauth signup, account-management, token-login or social-connect routes are mounted.
`/accounts/signup/`, `/accounts/login/`, `/accounts/email/`, `/accounts/social/signup/`
and `/accounts/google/login/token/` all return 404, and a test asserts that.

## Signup, in three steps

### 1. Submit — creates nothing

The signup form is unchanged: username, email, password, and either an organization name
or an invitation link. Submitting it stores one `PendingSignup` row and emails a code.
It does not create a user, an organization, a membership, or change an invitation.

`PendingSignup` has no organization and no user. Until verification completes there is no
tenant to own it, and inventing one would be a lie. It holds:

| Field | Notes |
| --- | --- |
| `email` | Unique and lowercased, so a later comparison cannot be defeated by case |
| `username`, `organization_name` | What was typed |
| `password_hash` | A `make_password()` hash. The submitted password is never stored |
| `palette`, `logo` | Branding, so it survives the verification detour |
| `invite_token_hash` | SHA-256 of the invitation. The token itself is not stored |
| `token` | `secrets.token_urlsafe(32)`, the unguessable handle the session uses |
| `code_hash`, `code_expires_at`, `code_attempts`, `code_sends`, `code_dead` | The emailed code, never the digits |
| `expires_at` | 24 hours |

### 2. Prove — Google or a code

Both methods lead to the same `complete()` call.

**By code.** Six digits, PBKDF2-hashed and salted with the pending signup's own token.
Valid 10 minutes, single use, dead after five wrong attempts, resendable after 60
seconds, capped at five per sliding hour. A correct guess is never penalised. Spending
the code is rolled back if the completion that follows fails.

**By Google.** `email_verified` must be the boolean `true`, and the claimed address must
match the pending signup case-insensitively. On a mismatch nothing is created, the
refusal is audited, and the page keeps offering the code. See the refusal message
`The Google account email does not match the email you signed up with`.

### 3. Complete — one transaction

Inside `transaction.atomic()`: lock the pending row, lock and check the invitation, create
the organization with its branding, create the user with the password already hashed,
create the secretary membership, consume the invitation, mark the address verified in
allauth, link the Google identity when that is how it was proven, write the audit events,
and delete the pending row. Any failure rolls all of it back, including the spent code.

Only after the commit does the browser sign in and land on `/`.

## Sign in

1. Press **Sign in with Google** on the login page, or open `/accounts/google/login/`.
2. Google returns to the shared callback, which matches the address to **exactly one**
   existing active secretary or auditor, links the Google identity on first use, and
   signs you in.

An address with no account is told `No account found for this Google email. Please sign
up first.` and nothing is created. Signing in can never complete a pending signup: the
login entry point clears the pending token from the session before it starts.

## Rules the backend enforces

Common to both flows:

1. Google must return `email_verified` as the boolean `true`. A string, a number or a
   missing flag is refused. (`verified_email` is accepted as an equivalent key.)
2. No access token or refresh token is stored. `SOCIALACCOUNT_STORE_TOKENS = False` and
   only `openid email profile` are requested, with `access_type=online` and PKCE on.
3. A client-supplied `scope`, `access_type`, `process` or redirect is discarded.
4. A successful sign-in lands on `/`. A `next` parameter is dropped, so there is no
   open redirect.
5. No `allauth` signup, confirmation, connection or notification view is reachable, and
   `SOCIALACCOUNT_AUTO_SIGNUP` is off.

Sign-in only:

6. The address must match **exactly one** existing user, case-insensitively. Zero matches
   and two or more matches are both refused.
7. The user must be active and hold an active `secretary` or `auditor` membership.
   A user with no membership, or with a role outside those two, is refused.
8. A Google identity that is already linked to one Duesdesk account may only ever
   authenticate that same account. One account may hold at most one Google identity.
9. A signed-in session belonging to a different account is never silently replaced.
10. An existing password is never read, changed or removed. A Google-only user with an
    unusable password can still set one through the ordinary password-reset email.

Verification only:

11. An address that already has an account is refused. Verifying is not a way in; only
    sign-in may open an existing account.
12. A Google identity already linked to any account is refused.
13. A pending signup is reachable only through the unguessable token in the session, so
    one browser cannot enumerate them by id or by email.

### Why the checks run in the provider, not only in `pre_social_login`

allauth resolves and refreshes the stored `SocialAccount` inside `SocialLogin.lookup()`,
which runs **before** `pre_social_login` and writes to the database. If the first refusal
happened only in `pre_social_login`, allauth would already have overwritten that stored
row. `google_auth/provider.py` therefore subclasses the Google OAuth adapter and validates
the claims in `complete_login`, which runs before `lookup()`. `views.google_login_callback`
builds allauth's own callback view around *our* adapter, so the subclass is the one that
runs. A claim we would refuse can never modify a linked identity.

### Housekeeping

`PendingSignup` rows expire after 24 hours. A management command removes them:

```text
python manage.py cleanup_pending_signups --dry-run
python manage.py cleanup_pending_signups
```

It only ever deletes rows past `expires_at`. Schedule it daily; nothing else depends on
it, because an expired row is already refused by `signup.current()`.

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

## Audit trail

Two tables, chosen so that neither is made to lie about who owns an event.

**`ledger.AuditEvent` — structurally unchanged.** Used for every event about a person who
is a member of an organization: `signup_email_verified`, `google_login_success` and
`google_account_linked`. Same `organization`, same `actor`, same `details` JSON shape as
every other ledger event. The Google events add a `method` key (`google` or `code`) and
the ordinary ledger events are written as before.

**`google_auth.GoogleAuthRejection` — new.** Used for an attempt that belongs to no
organization: an unknown address, an unverified claim, an address matching no user or two
users, an account with no membership, an OAuth error or cancellation, a provider with no
credentials configured, a refused verification, and a callback with no flow in the
session. Fields: `provider`, `action`, `reason`, `flow`, `method`, `email`, `user`
(nullable), `ip`, `created_at`.

This table deliberately has **no organization column and no organization foreign key**,
and no application view reads it. Nothing is filed under an organization that did not ask
for it. `user` is `SET_NULL`, so deleting an account does not erase the record.

`flow` says which entry point the attempt came from, so a refusal to verify a signup
address is never confused with a refusal to sign in. `method` says how the address was
being proven, `google` or `code`.

Note that the table stores the address Google presented, with the provider's own casing.
For an unknown sign-in that address belongs to a person who has no account here, so treat
the table as confidential operational data. `org:admin` permissions still apply; the table
is not exposed in the UI.

## Google Cloud setup

Do this in the Google Cloud console. Nothing below is optional except the local URIs.

1. Open <https://console.cloud.google.com/> and create (or pick) a project.
2. Open **Google Auth Platform** directly; no Gmail API is required for sign-in.
3. Open **Google Auth Platform → Branding** and fill in the app name and support email.
4. Open **Google Auth Platform → Audience**. Choose **External**. Set the publishing status
   to **In production** only when you are ready for arbitrary Google accounts to attempt a
   sign-in; while it is **Testing**, only the accounts listed under **Test users** can
   complete the consent step. Add your own Google address as a test user.
5. Open **Google Auth Platform → Clients → Create client**.
   - Application type: **Web application**.
   - Name: `Duesdesk` (or `Duesdesk local` for a separate local client).
   - **Authorized redirect URI**, exactly as written:
     - `https://duesdesk.onrender.com/accounts/google/login/callback/`
     - `http://localhost:10000/accounts/google/login/callback/`
     - `http://localhost:8000/accounts/google/login/callback/`
   - **Authorized JavaScript origins**: `http://localhost:10000` and `http://localhost:8000`
     (only needed for the local client).
6. Leave the client secret where the console puts it. Copy both the **Client ID** and the
   **Client secret** when the client is created — the secret is shown once.

There is one callback for both jobs, so one URI per host. Which local URI applies depends
on how you start the app:

| How you start it | Callback to register |
| --- | --- |
| `.\start.ps1` (runs `backend/serve.py`, `PORT` defaults to `10000`) | `http://localhost:10000/accounts/google/login/callback/` |
| `python backend/manage.py runserver 8000` | `http://localhost:8000/accounts/google/login/callback/` |

Register the exact strings you will use. Google compares the redirect URI verbatim, so a
different port or a missing trailing slash produces `redirect_uri_mismatch`.

## Supplying the credentials

Local — in `backend/.env` (this file is never committed):

```text
GOOGLE_CLIENT_ID=<client id from the console>
GOOGLE_CLIENT_SECRET=<client secret from the console>
```

Production — in the Render service `srv-daqkhjrktnus73b76rd0`, under **Environment**, add
both as **Secret** entries. Do not paste the values into a file, a commit, a chat message
or a screenshot. Rotate the secret in Google Cloud and update Render if it is ever exposed.

Without both values the flow fails closed before the browser is sent anywhere: the
reason `provider not configured` is recorded in `GoogleAuthRejection` and the page says
Google sign-in is not available on this deployment yet. The emailed code still works,
because it does not involve Google at all.

## Applying the migration

Local:

```text
$env:TEST_SQLITE='1'; $env:APP_ENV='local'
.venv\Scripts\python.exe backend\manage.py migrate google_auth
```

Production — one new table and one added column, no writes to any existing table. Run it
once per release, as a one-off Render job or from an operator shell with the production
environment injected:

```text
python manage.py migrate google_auth --noinput
```

`0001_initial` is already applied on Neon; `0002` and `0003` are not. Do not run
`migrate` from every worker at startup. `makemigrations --check` is clean, so nothing else
is pending.

After migrating, schedule the cleanup command once a day:

```text
python manage.py cleanup_pending_signups
```

## Trying it

With the code, from a signed-out browser — this needs no Google configuration at all:

1. Start the app (`.\start.ps1`).
2. Open `/signup/`, fill the form, submit.
3. You land on `/signup/verify/`. Nothing exists yet.
4. Read the code from the console email, type it, press **Verify email**.
5. You land on `/`, signed in. The organization, the membership and the verified address
   were all created in one transaction.
6. Sign out and sign in with the password you just chose.

Verification with Google:

1. From the same verification page, press **Continue with Google**.
2. Approve at Google. On a matching verified address you land on `/`, signed in, with the
   identity linked. On a mismatch you stay on the verification page with the reason shown
   and the code still available.

Sign-in, for an account created any other way:

1. Press **Sign in with Google**, or open `/accounts/google/login/` directly.
2. On success you land on `/`, already signed in, with the Google identity linked.
3. Sign out and sign in with the original password. It still works; Google did not replace it.

To confirm the stored links and refusals, query the tables directly:

```python
from allauth.socialaccount.models import SocialAccount
from allauth.account.models import EmailAddress
from google_auth.models import GoogleAuthRejection, PendingSignup
SocialAccount.objects.values_list('user_id', 'uid', 'extra_data')
EmailAddress.objects.filter(verified=True).values_list('user_id', 'email')
GoogleAuthRejection.objects.values_list('created_at', 'flow', 'method', 'reason', 'email', 'ip')
PendingSignup.objects.values_list('created_at', 'email', 'expires_at')
```

## Verification status

Run locally with `TEST_SQLITE=1` and `APP_ENV=local`. **Without `TEST_SQLITE=1`,
`backend/.env` points at the live Neon database; never run tests against it.**

```text
.venv\Scripts\python.exe backend\manage.py test --noinput
.venv\Scripts\python.exe backend\manage.py check
.venv\Scripts\python.exe backend\manage.py makemigrations --check --dry-run
.venv\Scripts\python.exe -m pip check
```

The Google round trip itself is mocked in the tests (`get_access_token_data` and
`_decode_id_token`), driven through the real URLs, the real session and the real
database. State, PKCE, replay, session rotation, flow separation, code expiry and
single-use, resend cooldown and caps, address mismatch, CSRF, invite binding and the
rollback of a failed completion are all covered. No test has spoken to Google, and a
successful real verification, a successful real sign-in and a real `redirect_uri_mismatch`
have all never been observed.

## UI changed by this release

- `templates/registration/login.html` — one `Sign in with Google` link, and the existing
  error paragraph now prefers a Google reason when there is one.
- `templates/registration/verify_email.html` — new: the code box, the Google button, the
  resend button, and **Wrong email? Go back**, which repopulates the signup form from the
  pending signup (username, email, organization name, colors, logo and invitation) but
  never the password.

`signup.html` keeps the same fields and gains no Google control; verification is reached
by submitting. No CSS, no JavaScript, including `static/signup.js`, and no other template
was touched.

## Things worth knowing

- **Going back does not trust the browser.** The back button posts nothing but
  `action=back`; the form is refilled from the `PendingSignup` row. Two things are not on
  that row and are held in the session for the length of the verification instead: the
  raw invitation token (only its hash is stored) and the logo preview token, which is
  signed for this session and expires after 30 minutes anyway.
- **A pending signup keeps the password hash for 24 hours.** That is deliberate — it is
  what lets verification finish with the password that was chosen — but it means the
  pending table holds a credential. It is cleaned up by the command above.