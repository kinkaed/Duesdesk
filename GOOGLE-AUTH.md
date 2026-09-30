# Google authentication (backend only)

Duesdesk uses Google for two separate things, and the two are deliberately kept apart:

| Flow | What Google is asked | What it may create |
| --- | --- | --- |
| **Verify before signup** | Prove you own the address you are about to register | Nothing at all. It writes one fact into your session. |
| **Sign in** | Open an account that already exists | Nothing at all. |

Google can never create a user, a membership, an organization or an invitation. The
account and the organization are still created by the ordinary secretary-only signup
logic — the only thing Google changes is that the address has been proven first.

## What exists today

| Item | State |
| --- | --- |
| Code and tests | Written and passing locally (184 tests, 2 skipped) |
| `google_auth.0001_initial` migration | Applied to Neon on 2026-09-30 |
| `google_auth.0002_googleauthrejection_flow` migration | **Written, not yet applied to Neon** |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Configured in Render, ignored by local `backend/.env` |
| Google OAuth client in Google Cloud | Created in project model-gearing-510212-u9; Testing audience |
| Google redirect | Verified live; account chooser opens. Final consent/callback still needs a real sign-in. |
| This two-flow release | **Not deployed to Render** |

Until the two environment variables are set, all four Google URLs refuse with the
ordinary page and an explanatory message, and nothing else changes. Password login,
secretary signup and every existing account keep working exactly as before.

## The four URLs

```text
POST /accounts/google/signup-verify/             start verification (from the signup form)
GET  /accounts/google/signup-verify/callback/    Google returns you here
GET  /accounts/google/login/                     start sign-in
GET  /accounts/google/login/callback/            Google returns you here
```

Which one is running is decided by the session and by nothing else. No query parameter,
form field or header can select a flow, so a callback aimed at the wrong route simply
refuses. Register all four in Google Cloud (see below).

No allauth signup, account-management, password-reset, token-login or social-connect
routes are mounted. `/accounts/signup/`, `/accounts/login/`, `/accounts/email/`,
`/accounts/social/signup/` and `/accounts/google/login/token/` all return 404, and a test
asserts that.

## The two flows

### Verify before signup

1. Type your email and press **Verify email with Google**. The control is an
   `<input type="submit" formaction="...">` inside the signup form, so the address you
   typed travels with it; `signup.js` selects `button[type=submit]` and is unaffected.
2. Google returns to the signup callback. The claims are checked (§Rules below) and,
   if they pass, your session records `verified_signup_email` and `signup_google_uid`.
   You are redirected back to `/signup/`, still signed out, now showing **Verified ✓**.
3. Submit the signup form. This is the ordinary form submission; it creates the user,
   the organization, the membership and marks the address verified and links the Google
   identity you just used.

The proof is deliberately narrow:

- It covers **one address**. Editing the email field after verifying invalidates it,
  because the check compares the stored address with the one being submitted.
- It lasts **15 minutes**.
- It is **single use**. It is cleared the moment the account exists.
- It matches case-insensitively, and a stored proof that is not a well-formed record
  is treated as no proof at all.

If you press the button and then leave without submitting, you have proven an address
and nothing else. No user, organization or membership exists.

### Sign in

1. Press **Sign in with Google** on the login page, or open `/accounts/google/login/`.
2. Google returns to the login callback, which matches the address to **exactly one**
   existing active secretary or auditor, links the Google identity on first use, and
   signs you in.

Refusals return the login page with a reason in its existing error slot. An address
that has no account is told to sign up first; an address that has one is told to sign in.

## Rules the backend enforces

Common to both flows:

1. Google must return `email_verified` as the boolean `true`. A string, a number or a
   missing flag is refused. (`verified_email` is accepted as an equivalent key.)
2. No access token or refresh token is stored. `SOCIALACCOUNT_STORE_TOKENS = False` and
   only `openid email profile` are requested, with `access_type=online` and PKCE on.
3. A client-supplied `scope`, `access_type`, `process` or redirect is discarded.
4. A successful sign-in lands on `/`. A `next` parameter is dropped, so there is no
   open redirect.

Sign-in only:

5. The address must match **exactly one** existing user, case-insensitively. Zero matches
   and two or more matches are both refused.
6. The user must be active and hold an active `secretary` or `auditor` membership.
   A user with no membership, or with a role outside those two, is refused.
7. A Google identity that is already linked to one Duesdesk account may only ever
   authenticate that same account. One account may hold at most one Google identity.
8. A signed-in session belonging to a different account is never silently replaced.
9. An existing password is never read, changed or removed. A Google-only user with an
   unusable password can still set one through the ordinary password-reset email.

Verification only:

10. An address that already has an account is refused. Verifying is not a way in; only
    sign-in may open an existing account.
11. A Google identity already linked to any account is refused.
12. The address Google returns must match the address typed into the form, or the
    attempt is refused.
13. Verification signs nobody in. The signup callback cannot open an account even when
    the claims are a perfect match for one, and the login callback cannot grant a proof.

### Why the checks run in the provider, not only in `pre_social_login`

allauth resolves and refreshes the stored `SocialAccount` inside `SocialLogin.lookup()`,
which runs **before** `pre_social_login` and writes to the database. If the first refusal
happened only in `pre_social_login`, allauth would already have overwritten that stored
row. `google_auth/provider.py` therefore subclasses the Google OAuth adapter and validates
the claims in `complete_login`, which runs before `lookup()`. A claim we would refuse can
never modify a linked identity. `test_linked_identity_cannot_switch_to_other_email_or_organization`
asserts that the stored `extra_data` is byte-for-byte unchanged after a refusal.

### IP addresses

Audit records use `request.META['REMOTE_ADDR']` only, validated as an IP address and
stored as text (empty when the value is malformed). Forwarded headers are never read:
`X-Forwarded-For`, `X-Real-IP` and `CF-Connecting-IP` are attacker-supplied unless a
trusted proxy rewrites them. `test_audit_ip_is_the_server_observation_not_a_forwarded_header`
asserts that a spoofed forwarded header does not appear in the record.

### Password lockout

`django-axes` still owns password lockout. `GuardedAuthenticationForm` checks
`AxesProxyHandler.is_allowed` before Django's own authentication, so the limit and the
locked screen are unchanged even though `ModelBackend` now runs first.

A successful **Google** sign-in is a different credential from the password, so it is not
refused by a password lockout and it clears that lockout (`AXES_RESET_ON_SUCCESS = True`).
`test_lockout_does_not_survive_a_google_sign_in` records this behaviour deliberately. If
you would rather have a locked account stay locked until the cool-off expires, say so and
it will be changed.

## Audit trail

Two tables, chosen so that neither is made to lie about who owns an event.

**`ledger.AuditEvent` — structurally unchanged.** Used for every event about a person who
is a member of an organization: `google_login_success`, `google_account_linked`,
`signup_email_verified_google`, and refusals of a known member. Same `organization`, same
`actor`, same `details` JSON shape as every other ledger event. The Google events add one
`flow` key (`login` or `signup`) to that JSON; nothing else about the table changes.

**`google_auth.GoogleAuthRejection` — new.** Used for an attempt that belongs to no
organization: an unknown address, an unverified claim, an address matching no user or two
users, an account with no membership, an OAuth error or cancellation, a provider with no
credentials configured, and a refused verification. Fields: `provider`, `action`,
`reason`, `flow`, `email`, `user` (nullable), `ip`, `created_at`.

This table deliberately has **no organization column and no organization foreign key**,
and no application view reads it. Nothing is filed under an organization that did not
ask for it. `user` is `SET_NULL`, so deleting an account does not erase the record.

`flow` says which entry point the attempt came from, so a refusal to verify a signup
address is never confused with a refusal to sign in.

Note that the table stores the address Google presented, with the provider's own casing.
For an unknown sign-in that address belongs to a person who has no account here, so treat
the table as confidential operational data. `org:admin` permissions still apply; the table
is not exposed in the UI.

The migrations are `backend/google_auth/migrations/0001_initial.py` (one new table) and
`0002_googleauthrejection_flow.py` (adds one `CharField(max_length=16)` to that table
only). Neither touches an existing table. The earlier allauth/sites migrations are
already applied to the live database.

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
   - **Authorized redirect URIs**, added one at a time, each exactly as written:
     - `https://duesdesk.onrender.com/accounts/google/login/callback/`
     - `https://duesdesk.onrender.com/accounts/google/signup-verify/callback/`
     - `http://localhost:10000/accounts/google/login/callback/`
     - `http://localhost:10000/accounts/google/signup-verify/callback/`
     - `http://localhost:8000/accounts/google/login/callback/`
     - `http://localhost:8000/accounts/google/signup-verify/callback/`
   - **Authorized JavaScript origins**: `http://localhost:10000` and `http://localhost:8000`
     (only needed for the local client).
6. Leave the client secret where the console puts it. Copy both the **Client ID** and the
   **Client secret** when the client is created — the secret is shown once.

Which local URI applies depends on how you start the app. Both callback paths are needed
in both cases, because the same client serves sign-in and verification:

| How you start it | Callback to register |
| --- | --- |
| `.\start.ps1` (runs `backend/serve.py`, `PORT` defaults to `10000`) | `http://localhost:10000/accounts/google/…/callback/` |
| `python backend/manage.py runserver 8000` | `http://localhost:8000/accounts/google/…/callback/` |

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

Without both values the flow fails closed: each Google URL refuses with the ordinary page
and the reason `provider not configured` is recorded in `GoogleAuthRejection`.

## Applying the migration

Local:

```text
$env:TEST_SQLITE='1'; $env:APP_ENV='local'
.venv\Scripts\python.exe backend\manage.py migrate google_auth
```

Production — one new column on one new table, no writes to any existing table. Run it once
per release, as a one-off Render job or from an operator shell with the production
environment injected:

```text
python manage.py migrate google_auth --noinput
```

`0001_initial` is already applied on Neon; `0002_googleauthrejection_flow` is not. Do not
run `migrate` from every worker at startup. `makemigrations --check` is clean, so nothing
else is pending.

## Trying it

Verification, from a signed-out browser:

1. Start the app (`.\start.ps1`) using the same hostname and port as the registered
   callback.
2. Open `/signup/`, type an email that has **no** Duesdesk account, and press
   **Verify email with Google**.
3. Approve at Google. You return to `/signup/` showing **Verified ✓** and still signed out.
4. Finish the form and submit. The account, the organization and the membership are created
   by the ordinary signup logic, and the address is marked verified.
5. Sign out and sign in with the password you just chose. It works.
6. Sign out again and press **Sign in with Google**. The identity linked during signup is
   reused, not duplicated.

Sign-in, for an account created any other way:

1. Press **Sign in with Google**, or open `/accounts/google/login/` directly.
2. On success you land on `/`, already signed in, with the Google identity linked.
3. Sign out and sign in with the original password. It still works; Google did not replace it.

To confirm the stored links and refusals, query the tables directly:

```python
from allauth.socialaccount.models import SocialAccount
from allauth.account.models import EmailAddress
from google_auth.models import GoogleAuthRejection
SocialAccount.objects.values_list('user_id', 'uid', 'extra_data')
EmailAddress.objects.filter(verified=True).values_list('user_id', 'email')
GoogleAuthRejection.objects.values_list('created_at', 'flow', 'reason', 'email', 'ip')
```

## Verification status

Run locally with `TEST_SQLITE=1` and `APP_ENV=local`. **Without `TEST_SQLITE=1`,
`backend/.env` points at the live Neon database; never run tests against it.**

```text
.venv\Scripts\python.exe backend\manage.py test ledger google_auth --noinput
.venv\Scripts\python.exe backend\manage.py check
.venv\Scripts\python.exe backend\manage.py makemigrations --check --dry-run
.venv\Scripts\python.exe -m pip check
```

The Google round trip itself is mocked in the tests (`get_access_token_data` and
`_decode_id_token`), driven through the real URLs, the real session and the real
database. State, PKCE, replay, session rotation, flow separation, the 15-minute expiry,
single use, address mismatch and CSRF are all covered. No test has spoken to Google, and
a successful real verification, a successful real sign-in and a real
`redirect_uri_mismatch` have all never been observed.

## Two things worth knowing

- **The Google control submits the whole signup form to `/accounts/google/signup-verify/`.**
  That is what `formaction` does. The password is sent to our own endpoint over the same
  TLS connection, is never read by the view and is never logged. It cannot be avoided
  without adding JavaScript, which was out of scope.
- **`GoogleAuthRejection` grew a column.** Anything that writes to that table by hand
  should set `flow`, otherwise a refusal is recorded without saying which entry point it
  came from.

## Configured testing account

The external OAuth application remains in Testing mode. The account owner
(kingsleyampoti4@gmail.com) is registered as a test user and has an active
Duesdesk membership. Start at https://duesdesk.onrender.com/accounts/google/login/, or
use **Verify email with Google** on https://duesdesk.onrender.com/signup/.
The deployment health and password login page both return HTTP 200.

## UI changed by this release

Two controls, and nothing else:

- `templates/registration/signup.html` — one `Verify email with Google` input and its
  `Verified ✓` state, plus a small error paragraph.
- `templates/registration/login.html` — one `Sign in with Google` link and the existing
  error paragraph now prefers a Google reason when there is one.

No CSS, no JavaScript and no other template was touched.
