# Duesdesk — React web app + Django + SQL Server

Your main React source is **work.tsx**. This is a browser application, not React Native.

## Where to edit

| Path | Purpose |
|---|---|
| `work.tsx` | React screens, forms, dialogs and application state |
| `styles.css` | Responsive layout, colours, typography |
| `api.ts` | Typed browser API client and data types |
| `main.tsx` | React entry point and error boundary |
| `backend/ledger/models.py` | Relational database schema |
| `backend/ledger/services.py` | Payment allocation, corrections, audit records |
| `backend/ledger/views.py` | Authenticated APIs, reports, imports and recovery |
| `backend/config/settings.py` | Local and production configuration |

## Use on this computer

Run `start.ps1`, then visit **http://127.0.0.1:8765/**. It uses the existing separate SQL Server database **FinancialSecretaryTest**. The original FINANCES database and Access file are not modified.

Local sample login: `secretary` / `TryDues25!`. These are test credentials only. Never deploy them or seed sample records in a live database.

After editing React, run `pnpm run build`, then restart `start.ps1` so production-style static assets refresh. There is no need to rebuild for routine member or payment changes.

## Fresh setup

Install Python 3.12 (available as `python`), Node 22.18 or newer, pnpm 11.19.0, Microsoft ODBC Driver 18 and SQL Server. These tools must be on PATH; verify `python --version`, `node --version` and `pnpm --version` in a new PowerShell window. Create an empty `FinancialSecretaryTest` database with SSMS and grant your Windows account access. Local defaults require no environment file; for a different database, copy `backend/.env.example` to `backend/.env` and adjust it before setup. Run `setup.ps1`. For new local test data only, run `.venv/Scripts/python.exe backend/manage.py seed_demo`.

Optional Vite development server: start the Django server first, then run `pnpm dev` and visit `http://127.0.0.1:5173/static/app/`. Sign in through the proxied `/login/` page, then return to that Vite URL. The default single-server build at port 8765 is the simplest way to use the app.

## Features

- Member registration, editing, search, status filters and profiles.
- GH₵25 monthly dues, partial payments, automatic advance allocations and historical monthly fees.
- One payment and one printable/PDF-save receipt; member name snapshots preserve original receipt identity.
- Void with a reason; original records remain visible and balances recalculate.
- Monthly collections, covered dues, monthly balances and cumulative arrears.
- Secretary, auditor and member roles enforced on the server; members see only their own data.
- Account creation and disabling, password changes, recovery email and login lockouts.
- Reviewed CSV imports for members/payments, duplicate-file prevention and transactional commits.
- CSV and Excel exports for monthly balances and payment ledgers.
- Audit history, CSRF protection, security headers, secure production cookies and encrypted SQL connections.

Read **USER-GUIDE.md** for workflows and **DEPLOYMENT.md** for online hosting, backups and go-live requirements.

## Checks

```powershell
pnpm run build
pnpm test
$env:TEST_SQLITE='1'
.venv/Scripts/python.exe backend/manage.py test ledger
Remove-Item Env:TEST_SQLITE
# Runs against a temporary SQL Server test database; requires database-creation privileges.
.venv/Scripts/python.exe backend/manage.py test ledger --noinput
.venv/Scripts/python.exe backend/manage.py check_finances
```

The app is not an accounting general ledger: it tracks membership contributions, not expenses, bank reconciliation, payroll or tax. Payment records are never hard-deleted through the app. Corrections use void-and-replace. Membership is billed continuously from the joining month through the optional last billable month; suspension by itself does not waive dues. Per-month exemptions and historical status schedules are not implemented. The fixed contribution is GH₵25; changing dues requires an administrator-controlled migration and review of existing advance payments.


## Release verification

This delivered folder is not currently a Git repository. A clean source-copy verification is possible, but a fresh-clone check and committed-secret history check require a repository. Exclude `.venv`, `node_modules`, `.env`, `.local-secret`, logs, databases, backups and generated static assets when publishing source. Production configuration is listed in DEPLOYMENT.md; there are no production credentials supplied.

A first non-demo secretary account can be created with `.venv/Scripts/python.exe backend/manage.py createsuperuser`. Do not run `seed_demo` against real data.
