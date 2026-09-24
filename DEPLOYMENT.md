# Hosting and go-live

This source is prepared for online deployment on **Render** (Docker web service) with **Neon PostgreSQL**. The connection string is a secret (`DATABASE_URL`). No cloud resources are purchased or deployed from this local build.

## Recommended setup: Render web service + Neon PostgreSQL

The two services are separate accounts. Keep both in the **Frankfurt** region. The Dockerfile builds the React frontend and the Django/Waitress backend with no Microsoft ODBC components; the app connects only to PostgreSQL.

1. **Create the Neon project** named `Duesdesk` in the Frankfurt (eu-central-1) region. Of the connection strings offered, use the **pooled** connection string (`-pooler.neon.tech` host) for the running app. A staff member with a regular (non-pooled) connection string can run migrations and checks.
2. **Create a Render web service** `duesdesk` from the `kinkaed/Duesdesk` repository, `runtime: docker` (the supplied `render.yaml` does this), using the updated Dockerfile. Set the health-check route `/health/` and HTTPS Only.
3. **Configure secrets.** Put `DATABASE_URL` and every value in the environment list below into Render secret settings. Never commit real values. Use a new random `DJANGO_SECRET_KEY`, your live hostname, trusted origins and SMTP credentials.
4. **Bootstrap the database** once per release with a migration step, then start the app (see "Build and first deployment").
5. **Credentials in the browser** are never used; the database is reached only from the Django service using `DATABASE_URL`.

Neon documentation: https://neon.tech/docs/
Render documentation: https://render.com/docs/web-services

## Required production environment

```text
APP_ENV=production
DATABASE_URL=postgresql://<user>:<password>@<project>-pooler.eu-central-1.aws.neon.tech/<dbname>?sslmode=require
DJANGO_SECRET_KEY=<random 64+ character secret>
ALLOWED_HOSTS=dues.example.org,duesdesk.onrender.com
CSRF_TRUSTED_ORIGINS=https://dues.example.org,https://duesdesk.onrender.com
EMAIL_HOST=<SMTP host>
EMAIL_PORT=587
EMAIL_HOST_USER=<SMTP user>
EMAIL_HOST_PASSWORD=<SMTP secret>
EMAIL_USE_TLS=1
DEFAULT_FROM_EMAIL=Duesdesk <noreply@example.org>
BIND_HOST=0.0.0.0
PORT=8000
TRUST_PROXY=1
TRUSTED_PROXY_IP=*
```

`TRUSTED_PROXY_IP=*` is appropriate only behind a hosting ingress that blocks direct access and strips/replaces incoming forwarded headers. Otherwise set the exact trusted proxy IP. The app trusts only forwarded protocol, not client-supplied hostnames. Require HTTPS and keep allowed hosts explicit. Configure SMTP domain verification (SPF/DKIM) with your mail provider; test actual email delivery, because configured settings alone do not prove recovery works.

## Build and first deployment

```text
docker build -t duesdesk:1.0 .
```

Run these commands once against the fresh Neon database, with the secret environment injected (Render's one-off job or an operator shell). Never run `seed_demo` against real data.

```text
python manage.py migrate --noinput
python manage.py createsuperuser
```

In a shell set the organisation (replace the examples):

```python
from ledger.models import Organisation
Organisation.objects.update_or_create(pk=1, defaults={
    'name': 'Your Organisation',
    'contact': 'Your public contact information',
    'receipt_footer': 'Thank you for your contribution.',
})
```

Then run `python manage.py deployment_check`. The regular image command runs this check before starting the server. Migrations are deliberately **not** run by every worker at startup; run them once per release with a migration identity and a verified recovery point first.

## Transferring existing records

The old SQL Server database is retired. Existing records can be copied once, before the app goes live, using two one-off migration commands included in this source:

```text
# Read-only export from SQL Server (requires pyodbc and ODBC Driver 18 on the operator machine).
pip install -r backend/requirements-legacy.txt
python backend/manage.py export_legacy          # writes backend/legacy_dump.json
# Load into the configured PostgreSQL database and advance identity sequences.
python backend/manage.py import_legacy --force
```

`legacy_dump.json` is gitignored and contains member, payment and account data; keep the file private and delete it after a verified transfer. Without real data to keep, start fresh and skip this step.

## Runtime permissions and audit protection

Use a separate restricted PostgreSQL role for the running app. Neon supports creating roles and granting privileges per database. Grant the runtime role table access and the ability to read sequences, but not schema-alter permission. Append-only tables are locked down:

```sql
-- Repeat for the ledger_* and auth_* tables the app writes.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO DuesdeskRuntime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO DuesdeskRuntime;
```
```sql
-- Append-only through the app: no UPDATE or DELETE by the runtime role.
REVOKE UPDATE, DELETE ON ledger_auditevent, ledger_importbatch, ledger_allocation, ledger_duesmonth FROM DuesdeskRuntime;
REVOKE DELETE ON ledger_payment, ledger_member FROM DuesdeskRuntime;
GRANT UPDATE ON ledger_payment TO DuesdeskRuntime; -- voiding writes voided_* columns
```

Payment updates are required for voiding. The API does not permit changing original amounts or allocations. Database administrators can still bypass application controls; protect and monitor those accounts. Do not apply these restrictions to the migration owner, which needs to evolve the schema.

## Backups and recovery

- Neon provides automatic point-in-time recovery and branch restore. Review the retention plan for the `Duesdesk` project and document your recovery objective (how much data can be lost, and how quickly service must resume).
- Before go-live, restore a Neon backup/branch to a separate database, point a non-public app instance at it, and run `check_finances`. Verify at least one old receipt and member statement.
- Before a schema upgrade, take/verify a recovery point. Keep the prior image version. Some migrations cannot safely be rolled back automatically; restore to a separate database and switch only after reconciliation.
- Do not store backup files under public static folders or in Git. A CSV export is not a complete backup.

Neon backup guidance: https://neon.tech/docs/manage/backups

## Acceptance checklist

1. `pnpm run build` and `python manage.py test ledger` pass. Backend tests run against PostgreSQL in CI (`postgres-tests` job).
2. `python manage.py check --deploy --fail-level WARNING`, `deployment_check` and `check_finances` pass under live configuration.
3. No demo account/password or fictional member/payment records are present in the live database.
4. HTTPS certificate, domain, redirect, cookies and CSRF origins work at the real URL.
5. Sign in as secretary, auditor and a linked member; confirm read-only and own-record boundaries.
6. Record a controlled test payment, verify allocations and receipt, void it with a reason, and reconcile the resulting report.
7. Test password recovery delivery and link expiry, lockout and session logout.
8. Test CSV import preview/commit and Excel download in the real hosted environment.
9. Configure health/error alerts, monitor database connectivity and storage, and run scheduled `check_finances` plus `clearsessions`. Review login attempts and audit logs; establish a retention policy for security logs and recovery throttling records.
10. Perform and document the restore drill. Assign named people responsibility for access reviews, updates and incidents.

This is a deployable application with tested core controls, not a claim of an independent security audit. Domain, SMTP, cloud networking, Neon roles/backups, and container operation must be verified in the selected hosting account. No hosting bill or public deployment is initiated by the local build.