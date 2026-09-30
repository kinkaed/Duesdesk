# Google sign-in (backend only)

Duesdesk can accept a Google identity for an account that **already exists**. Google
can never create a user, a membership or an organization. There is no Google button and
no template, CSS or JavaScript was changed: the flow is started by opening one URL.

## What exists today

| Item | State |
| --- | --- |
| Code and tests | Written and passing locally |
| `google_auth.0001_initial` migration | Applied to Neon on 2026-09-30 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Configured in Render and ignored local backend/.env |
| Google OAuth client in Google Cloud | Created in project model-gearing-510212-u9; Testing audience |
| End-to-end test against real Google | **Not done** |
| Deployed to Render | **Not done** |

Until the two environment variables are set, both Google URLs return the ordinary login
page with an error and nothing else changes. The password login, the secretary signup and
every existing account keep working exactly as before.

## The only two URLs

```text
GET /accounts/google/login/          start the flow (redirects to Google)
GET /accounts/google/login/callback/ Google returns the user here
```

No allauth signup, account-management, password-reset, token-login or social-connect
routes are mounted. `/accounts/signup/`, `/accounts/login/`, `/accounts/email/`,
`/accounts/social/signup/` and `/accounts/google/login/token/` all return 404, and a test
asserts that.

## Rules the backend enforces

1. Google must return `email_verified` as the boolean `true`. A string, a number or a
   missing flag is refused. (`verified_email` is accepted as an equivalent key.)
2. The address must match **exactly one** existing user, case-insensitively. Zero matches
   and two or more matches are both refused.
3. The user must be active and hold an active `secretary` or `auditor` membership.
   A user with no membership, or with a role outside those two, is refused.
4. A Google identity that is already linked to one Duesdesk account may only ever
   authenticate that same account. One account may hold at most one Google identity.
5. A signed-in session belonging to a different account is never silently replaced.
6. An existing password is never read, changed or removed. A Google-only user with an
   unusable password can still set one through the ordinary password-reset email.
7. No access token or refresh token is stored. `SOCIALACCOUNT_STORE_TOKENS = False` and
   only `openid email profile` are requested, with `access_type=online` and PKCE on.
8. After a successful sign-in the user is redirected to `/`. A `next` parameter is
   discarded, so there is no open redirect.

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

**`ledger.AuditEvent` — unchanged.** Used for every event about a person who is a member
of an organization: successful Google sign-in, first-time linking, and refusals of a known
member. Same `organization`, same `actor`, same `details` JSON shape as every other ledger
event.

**`google_auth.GoogleAuthRejection` — new.** Used for an attempt that belongs to no
organization: an unknown address, an unverified claim, an address matching no user or two
users, an account with no membership, an OAuth error or cancellation, and a provider with
no credentials configured. Fields: `provider`, `action`, `reason`, `email`, `user`
(nullable), `ip`, `created_at`.

This table deliberately has **no organization column and no organization foreign key**,
and no application view reads it. Nothing is filed under an organization that did not
ask for it. `user` is `SET_NULL`, so deleting an account does not erase the record.

Note that the table stores the address Google presented. For an unknown sign-in that
address belongs to a person who has no account here, so treat the table as confidential
operational data. `org:admin` permissions still apply; the table is not exposed in the UI.

The migration is `backend/google_auth/migrations/0001_initial.py`. It creates one table and
touches nothing else. The earlier allauth/sites migrations are already applied to the live
database.

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
     - `http://localhost:10000/accounts/google/login/callback/`
     - `http://localhost:8000/accounts/google/login/callback/`
   - **Authorized JavaScript origins**: `http://localhost:10000` and `http://localhost:8000`
     (only needed for the local client; production serves no JavaScript origin because
     there is no button).
6. Leave the client secret where the console puts it. Copy both the **Client ID** and the
   **Client secret** when the client is created — the secret is shown once.

Which local URI applies depends on how you start the app:

| How you start it | Callback to register |
| --- | --- |
| `.\start.ps1` (runs `backend/serve.py`, `PORT` defaults to `10000`) | `http://localhost:10000/accounts/google/login/callback/` |
| `python backend/manage.py runserver 8000` | `http://localhost:8000/accounts/google/login/callback/` |

Register the exact string you will use. Google compares the redirect URI verbatim, so a
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

Without both values the flow fails closed: `/accounts/google/login/` returns 403 with the
ordinary login page, and each refusal is recorded in `GoogleAuthRejection` with reason
`provider not configured`.

## Applying the migration

Local:

```text
$env:TEST_SQLITE='1'; $env:APP_ENV='local'
.venv\Scripts\python.exe backend\manage.py migrate google_auth
```

Production — one migration, one new table, no writes to any existing table. Run it as a
one-off Render job or from an operator shell with the production environment injected:

```text
python manage.py migrate google_auth --noinput
```

Do not run `migrate` from every worker at startup; run it once per release, as
`DEPLOYMENT.md` describes. `makemigrations --check` is clean, so nothing else is pending.

## Trying it

1. Start the app (`.\start.ps1`) using the same hostname and port as the registered callback.
2. Sign in with a Google account whose address already belongs to an active secretary or
   auditor in Duesdesk. If none does yet, create the account through `/signup/` first.
3. Open `http://localhost:10000/accounts/google/login/` directly. There is no button.
4. On success you land on `/`, already signed in, with the Google identity linked to the
   existing account.
5. Sign out and sign in with the original password. It still works; Google did not replace it.

To confirm the stored links and refusals, query the tables directly:

```python
from allauth.socialaccount.models import SocialAccount
from allauth.account.models import EmailAddress
from google_auth.models import GoogleAuthRejection
SocialAccount.objects.values_list('user_id', 'uid', 'extra_data')
EmailAddress.objects.filter(verified=True).values_list('user_id', 'email')
GoogleAuthRejection.objects.values_list('created_at', 'reason', 'email', 'ip')
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
`_decode_id_token`). State, PKCE, replay, session rotation and CSRF are covered, but no
test has spoken to Google. A successful real sign-in and a real `redirect_uri_mismatch`
have both never been observed.
