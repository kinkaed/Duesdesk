# Hosting and go-live

This source is prepared for online deployment. No cloud resources have been purchased or deployed, and the Docker image has not been built on this Windows machine because Docker is unavailable. Complete the host checks below before treating the application as live.

## Recommended: Azure App Service + Azure SQL Database

This preserves SQL Server compatibility and keeps the app and database with one provider. Microsoft documents Django with `mssql-django`, ODBC drivers and managed identity:

https://learn.microsoft.com/en-us/sql/connect/python/mssql-django/deploy-azure-app-service

1. Create a resource group, an Azure SQL logical server and a **fresh** database named `DuesdeskLive`. Choose an appropriate region, configure network access and use a paid service tier suitable for the workload. Use the Azure pricing calculator for the combined application/database cost; they are separate charges.
2. Build the supplied Dockerfile in CI or a machine with Docker and publish it to a private container registry. Create an App Service container app with port 8000. Set HTTPS Only and the health-check route `/health/`.
3. Configure the environment variables below in App Service application settings / secret references. Never commit real secrets. Use a new secret key, live hostname, trusted origins and SMTP credentials.
4. Prefer `DB_AUTH=managed_identity`. Enable the app's system-assigned managed identity and grant it data-reader/data-writer rights in Azure SQL. Use a separate deployment principal for schema changes. With SQL authentication, set `DB_AUTH=sql`, `DB_USER` and `DB_PASSWORD` instead.
5. Run the one-time bootstrap commands below as a deployment job with the migration principal. The app intentionally will not start with incomplete live configuration or the known demo password.
6. Switch to the restricted runtime database identity, configure the app's secret settings, start the container and verify the full acceptance checklist.

Do not expose the SQL Server running on your laptop to the internet. Azure SQL is the hosted replacement. Import approved real data using the app after setup; do not seed or copy fictional test records.

## Alternative: Render Docker web service + external Azure SQL

Render supports a Docker web service with HTTPS and custom domains. Use the supplied `render.yaml` with a paid web service; its database is **external Azure SQL**, not Render Postgres. Set SQL authentication credentials as secrets and permit only the app's outbound network addresses to reach Azure SQL. Both providers bill separately. Run migrations/bootstrap as a one-off container command before the normal startup check. This option is convenient if you prefer Render's Git-based deployment, but involves two providers.

https://render.com/docs/web-services
https://render.com/docs/deploys
https://render.com/pricing

Free services that sleep are not recommended for an operational financial secretary system. Vercel/Netlify can host a separate React frontend, but this project also needs a Django service and hosted SQL Server; splitting it adds authentication and deployment complexity without a benefit for this small app.

## Required production environment

```text
APP_ENV=production
DJANGO_SECRET_KEY=<random 64+ character secret>
ALLOWED_HOSTS=dues.example.org,your-app.azurewebsites.net
CSRF_TRUSTED_ORIGINS=https://dues.example.org,https://your-app.azurewebsites.net
DB_HOST=your-server.database.windows.net
DB_NAME=DuesdeskLive
DB_PORT=1433
DB_AUTH=managed_identity
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

`TRUSTED_PROXY_IP=*` is appropriate only behind a hosting ingress that blocks direct access and strips/replaces incoming forwarded headers. Otherwise set the exact trusted proxy IP. The app trusts only forwarded protocol, not client-supplied hostnames. Require HTTPS and keep allowed hosts explicit. Configure SMTP domain verification (SPF/DKIM) with your mail provider. Test actual email delivery; configured settings alone do not prove recovery works.

Production connections require a verified SQL Server certificate. Local testing uses the local SQL Server self-signed certificate only. Browser code never contains database credentials.

## Build and first deployment

```text
docker build -t duesdesk:1.0 .
```

Run these commands inside the image using your host's secret injection. `--env-file` is an option for a local operator; keep such a file out of source control and restrict its filesystem permissions.

```text
docker run --rm --env-file live.env duesdesk:1.0 python manage.py migrate --noinput
docker run --rm -it --env-file live.env duesdesk:1.0 python manage.py createsuperuser
docker run --rm -it --env-file live.env duesdesk:1.0 python manage.py shell
```

In the shell set the organisation (replace the examples):

```python
from ledger.models import Organisation
Organisation.objects.update_or_create(pk=1, defaults={
    'name': 'Your Organisation',
    'contact': 'Your public contact information',
    'receipt_footer': 'Thank you for your contribution.',
})
```

Then run `python manage.py deployment_check`. The regular image command runs this check before starting the server. Migrations are deliberately **not** run by every worker at startup; run them once per release with a migration identity and a backup first.

## Runtime permissions and audit protection

Use a separate restricted database user for the running app. It needs select/insert/update/delete on session, auth and login-attempt tables and the relevant application writes. It does not need permission to change the schema. Restrict updates/deletes on the append-only tables after migrations:

```sql
-- Replace DuesdeskRuntime with your actual runtime database user.
DENY UPDATE, DELETE ON dbo.ledger_auditevent TO [DuesdeskRuntime];
DENY UPDATE, DELETE ON dbo.ledger_allocation TO [DuesdeskRuntime];
DENY DELETE ON dbo.ledger_payment TO [DuesdeskRuntime];
DENY DELETE ON dbo.ledger_member TO [DuesdeskRuntime];
DENY UPDATE, DELETE ON dbo.ledger_importbatch TO [DuesdeskRuntime];
DENY UPDATE, DELETE ON dbo.ledger_duesmonth TO [DuesdeskRuntime];
```

Payment updates are required for voiding. The API does not permit changing original amounts or allocations. Database administrators can still bypass application controls; protect and monitor those accounts. Do not apply these denies to the migration owner, which needs to evolve the schema.

## Backups and recovery

- Configure Azure SQL automatic backup retention and point-in-time recovery to meet your organisation's needs. Retention, storage and longer-term backups can change the cost.
- Document the recovery objective (how much data can be lost, and how quickly service must resume).
- Before go-live, restore a backup to a separate database, point a non-public app instance at it, and run `check_finances`. Verify at least one old receipt and member statement.
- Before a schema upgrade, take/verify a recovery point. Keep the prior image version. Some migrations cannot safely be rolled back automatically; restore to a separate database and switch only after reconciliation.
- For local SQL Server, schedule native SQL Server backups using your database administrator's tools. A CSV export is not a complete backup.
- Do not store backup files under public static folders or in Git. Encrypt them, restrict access and document retention.

Microsoft backup guidance: https://learn.microsoft.com/en-us/azure/azure-sql/database/automated-backups-overview

## Acceptance checklist

1. `pnpm run build` and `python manage.py test ledger` pass. Run backend tests against a temporary SQL Server database too.
2. `python manage.py check --deploy --fail-level WARNING`, `deployment_check` and `check_finances` pass under live configuration.
3. No demo account/password or fictional member/payment records are present in the live database.
4. HTTPS certificate, domain, redirect, cookies and CSRF origins work at the real URL.
5. Sign in as secretary, auditor and a linked member; confirm read-only and own-record boundaries.
6. Record a controlled test payment, verify allocations and receipt, void it with a reason, and reconcile the resulting report.
7. Test password recovery delivery and link expiry, lockout and session logout.
8. Test CSV import preview/commit and Excel download in the real hosted environment.
9. Configure health/error alerts, monitor database connectivity and storage, and run scheduled `check_finances` plus `clearsessions`. Review login attempts and audit logs; establish a retention policy for security logs and recovery throttling records.
10. Perform and document the restore drill. Assign named people responsibility for access reviews, updates and incidents.

This is a deployable application with tested core controls, not a claim of an independent security audit. Domain, SMTP, cloud networking, actual backup scheduling/restoration, and container operation must be verified in the selected hosting account. No hosting bill or public deployment is initiated by the local build.
