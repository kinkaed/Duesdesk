# Organizations, invitations and branding

Implemented in the React web application and Django API. Accounts have one organization membership today; switching organizations means signing out and signing into the other organization's account. There is no multi-organization account switcher. The separate UserAccess membership model permits extending this later without adding organization fields to Django's user model.

## Schema

Django retains the existing British-spelled `ledger_organisation` table to preserve current data. This is the system's organizations table.

| Table | Columns / constraints |
|---|---|
| `ledger_organisation` | `id` bigint PK; `public_id` UUID unique; `name` varchar(120); `created_at` timestamp; `contact` varchar(200); `receipt_footer` varchar(250); `logo` bytea (normalized PNG); `primary`, `secondary`, `accent` varchar(7), validated six-digit hex colors |
| `ledger_useraccess` | `id` PK; `user_id` unique FK to auth_user; `organization_id` required FK; `role` secretary/auditor/member; `active` boolean; optional unique member_id FK |
| `ledger_secretaryinvite` | `id` PK; required `organization_id` FK; `token_hash` char-compatible varchar(64), unique SHA-256 digest; `email` varchar(254); `created_by_id` FK; `created_at`, `expires_at`; nullable `used_at`, `used_by_id`, `revoked_at` |
| `ledger_member`, `ledger_payment`, `ledger_allocation`, `ledger_duesmonth`, `ledger_auditevent`, `ledger_importbatch` | Each has required indexed `organization_id` FK with protected deletion |
| `ledger_duesmonth` | Unique `(organization_id, month)`; monthly rate belongs only to that organization |
| `ledger_importbatch` | Unique `(organization_id, digest)`; importing the same file into another organization does not conflict |

Member/receipt IDs remain globally unique. This preserves existing receipt references and is not an authorization mechanism. Payment amounts and allocation rules remain unchanged (GH₵25/month). Existing member/auditor roles continue to work within their organization. The secretary UI adds secretaries through invitations.

Logos are small database-backed objects, served by `/organizations/<public UUID>/logo/`. Keeping them in PostgreSQL avoids losing uploads when Render restarts and includes them in database backups. There is no extra storage service or storage key to configure. Organization names, palettes and logos on branded sign-in/invitation pages are intentionally public; financial records and account lists remain private.

## Signup and invitations

1. `/signup/` without an invitation asks for account details, organization name, optional logo, and editable primary/secondary/accent colors. One transaction creates the organization, user, active secretary membership and audit record, then signs the secretary in.
2. In **Manage → Invite Secretary**, enter the intended secretary's email and generate a link. Copy and share it privately. The app does not send this invitation by email.
3. The server generates 32 random bytes with `secrets.token_urlsafe(32)`. Only the SHA-256 digest is stored. The raw link is returned once and expires in seven days.
4. `/signup/?invite=<token>` names the organization and requires the invited email. The user sets their own password. Organization and role come from the invitation, never from submitted organization/role fields.
5. Redemption locks the organization and invitation, rechecks validity, creates the user and membership and records `used_at`/`used_by_id` atomically. A used, expired or revoked token is rejected. Secretaries can revoke unused invitations belonging to their organization.

Invitations onboard new accounts. Existing usernames/emails cannot be registered again. Leaving retains the account and an inactive membership; reactivation currently requires a trusted operator using `assign_access` for that same organization. Changing an existing account to a different organization is deliberately refused. Use a separate account for another organization.

## Leave and handover

**Manage → Leave Organization** asks for confirmation. It succeeds only when another active secretary with an active login exists in the same organization. It deactivates the departing membership, revokes their pending invitations, logs the action and signs them out. Existing sessions fail their next protected request. Member, payment, allocation, receipt and audit records remain intact.

Organization-row locks serialize leave, disable, payment/import writes and invitation redemption on PostgreSQL. Two secretaries cannot concurrently leave the organization empty through these API flows. An operator with direct database access remains responsible for not disabling the final secretary outside the app.

## Branding implementation

- Pillow decodes PNG/JPEG/WebP, rejects files over 1 MB or over four million pixels, applies EXIF orientation, resizes to at most 256×256, and writes a fresh PNG without source metadata.
- An eight-color median-cut palette ranks prominent colors. A saturated prominent color becomes primary; a second becomes accent (or primary when only one exists); secondary is a pale tint. Transparent pixels are composited on white for palette extraction.
- Upload preview returns a signed, session/organization-bound token valid for 30 minutes plus suggested hex values. Preview does not change saved branding. The user can review and adjust colors before saving.
- Primary/secondary/accent CSS variables are loaded from the authenticated organization. Major buttons, summary cards, selected navigation, accents and logo update after saving. Semantic paid/partial/unpaid colors remain consistent.
- Text on themed surfaces chooses whichever of black or white gives better contrast (at least 4.5:1 for the selected opaque sRGB background).
- Missing branding uses existing defaults: primary `#214f43`, secondary `#edf4e6`, accent `#527735`. Invalid historical values fall back safely when rendering.
- **Manage → Branded sign-in page** opens `/login/?org=<public UUID>` with that organization's name/logo/theme. The generic login uses the default brand because no organization has been selected yet. Signing in always loads the account's actual organization, regardless of the login URL.

## Query audit and safeguards

Before this change, member/payment/report/account/settings queries were global and staff users could bypass membership checks. No two-organization isolation existed.

After this change, visible_members/visible_payments require an active membership. All authenticated data routes derive organization from that membership. Member details, payment previews/writes/voids, receipts, single-member statements, monthly CSV/Excel, imports, accounts, audit logs and settings are scoped. Spoofed organization IDs cannot change the tenant. CSV preview tokens bind both user and organization. The global staff/superuser fallback has been removed.

Model save validation rejects cross-organization payment/member, allocation/payment/month and membership/member relationships and prevents moving records between organizations. `check_finances` also detects mismatches introduced by direct SQL or bulk ORM operations, which bypass model validation. Tenant access is enforced in the application; PostgreSQL row-level security is not configured. Database credentials and operator commands must remain private.

Global identity lookups for login, duplicate usernames/emails and password recovery are intentional. Maintenance/reconciliation commands are operator-only and can inspect all tenants. They are not web endpoints. The old SQL Server import command refuses to run against the new schema; legacy imports must be completed on the old release in a separate database before applying organization migrations.

## Existing data migration and rollout

Migrations 0005 and 0006 add nullable organization fields, assign existing data/users to the default organization (existing ID 1), then require those fields. Existing organization name/contact/footer and user roles are retained. Missing access records are assigned to the default organization; existing migration 0004 already repairs old users. PostgreSQL identity sequences are advanced to avoid a conflict on the next signup.

The migration cannot infer which historical records belonged to different real-world organizations. Existing shared data remains together in the default organization. Splitting historical records requires an explicitly reviewed data mapping; this release does not guess ownership.

Before production rollout:

1. Back up Neon and verify a restore path. Test this migration against a staging copy.
2. Run the PostgreSQL CI job, including concurrency tests. Local SQLite tests do not prove PostgreSQL locking behavior.
3. Pause writes during migration and application cutover. Old application code must not continue handling requests after required organization fields are introduced. Render build-time migration alone does not guarantee this maintenance window.
4. Build React, collect static assets and verify the manifest using the existing Render build steps. Apply `python backend/manage.py migrate --noinput` once.
5. Run `python backend/manage.py check_finances`; sign in to the existing secretary account, verify counts/totals/receipts, create a separate test organization and check isolation.
6. Rollback by restoring the pre-migration backup together with the previous application version. Do not reverse these migrations after new organizations have been created.

The application changes have not been deployed by this feature implementation. Existing Render host, port, manifest storage, proxy and Neon configuration are unchanged. No new environment variables are required.

## Verification

Automated tests cover organization A/B reads, exports, receipts, report downloads, cross-organization writes, invitation spoofing/revocation/reuse/expiry/email binding, staff bypass removal, leave/disabled sessions, per-organization rates/import digests, logo validation/session binding, color contrast, and preservation of legacy IDs/roles/payment values. PostgreSQL-only tests cover simultaneous leaves and simultaneous invitation redemption.

Manual browser checks on an isolated temporary database confirmed: A shows only Member A, B shows only Member B; signing out of B and into A changes yellow/black buttons and cards back to blue/white and replaces the logo/name; changing a color saves immediately; invitation link generation works; leaving as the sole secretary shows a clear refusal. The preview uses the compiled React bundle, Waitress and manifest-backed WhiteNoise assets.

Run checks from the repository root:

```powershell
pnpm run build
pnpm test
$env:TEST_SQLITE='1'
.venv/Scripts/python.exe backend/manage.py test ledger --noinput
.venv/Scripts/python.exe backend/manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe backend/manage.py collectstatic --noinput
.venv/Scripts/python.exe backend/manage.py verify_static
Remove-Item Env:TEST_SQLITE
# Against a disposable PostgreSQL test server with CREATE DATABASE rights:
.venv/Scripts/python.exe backend/manage.py test ledger --noinput
```

Operator recovery for an existing account (same organization only):

```powershell
.venv/Scripts/python.exe backend/manage.py assign_access USERNAME --role secretary --organization-id ORGANIZATION_ID
```

A `createsuperuser` account has no application access until explicitly assigned to an organization. Normal secretaries should use signup or an invitation.
