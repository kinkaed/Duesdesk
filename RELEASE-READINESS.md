# Release readiness review — 24 September 2026

Verdict: NOT approved for a public production release yet. Local release checks pass; hosted production acceptance remains incomplete.

## 1. Environment and documentation
- The delivery folder is not a Git repository. Tested a separate source copy with a new Python virtual environment and new node_modules. No fresh-clone or Git-history claim is possible.
- Frozen pnpm install, TypeScript/Vite production build, pinned Python install, pip check, empty SQLite migrations, collectstatic and migration drift check passed.
- Required local prerequisites: Python 3.12 on PATH, Node 22.18+, pnpm 11.19.0, PostgreSQL, an existing database and database permissions. This machine uses bundled Python; a normal `python` command was not found on PATH during inspection.
- README now explains prerequisites, local env configuration, first secretary creation and source publication exclusions.
- setup.ps1 now fails if static collection fails instead of announcing success.
- Missing for production: hosting account, live SQL endpoint/identity, domain/origins, random secret, SMTP configuration. DEPLOYMENT.md enumerates these.

## 2. Automated tests
- Existing suite: 28 passed, 0 failed in the clean SQLite environment.
- Expanded backend suite: 33 passed, 0 failed on SQLite and SQL Server. SQL Server tests used the framework's temporary test database.
- Added five backend tests: malformed request safety, invalid payment rollback, import rollback, database outage health response, arrears vs advance allocations.
- Strengthened import rollback test to fail on its second row; all five release tests passed again on SQL Server after this change.
- Added five frontend API tests: CSRF writes, network rejection, expired-session redirect, non-JSON server error, validation/permission errors. 5 passed, 0 failed.
- After the approved preview fix: full SQL Server suite 34 passed, 0 failed; frontend suite remains 5 passed, 0 failed.
- No flakiness observed in these runs; this is not a long-duration or concurrency stress test.

## 3. Functional checks
Using a separate SQLite-backed Waitress server on port 8766 with fictional data:
- Browser sign-in, member creation/search, GH₵100 payment preview/save, four GH₵25 allocations, receipt rendering, member profile and Excel download event passed.
- Voiding preserved the receipt and updated reports. Monthly report screen and no-match member search rendered correctly. Sign-out returned to login.
- Test ledger reconciled with check_finances.
- Monthly report file contents, import and account controls have automated coverage, but monthly download/import/account-management flows were not exhaustively repeated through the browser.
- Actual PDF print output, password-reset email delivery and reset UI completion remain unverified.
- No changes were made to existing member/payment records in the user's working database.

## 4. Error handling
- Invalid amounts, malformed JSON, invalid payment fields, expired sessions, CSRF, role restrictions, missing members and formula-safe exports are covered.
- Database failure returns a minimal 503 health response. Frontend handles rejected network/server requests as errors, not successful writes.
- Approved fix implemented: import preview now rejects references over 80 characters and notes over 500 characters, reporting the CSV row number. Added a regression test. Import rollback test now simulates a membership billing cutoff changing between preview and commit, with the second row failing.
- A hanging network request has no application-level timeout; this remains an improvement candidate requiring behavior approval, not a verified crash.

## 5. Security and configuration
- Focused source scan found no live credentials or private keys. No Git repository exists, so committed-secret history cannot be checked.
- Demo password is deliberately present in seed/documentation and rejected by deployment_check for active production accounts. Tests contain fictional passwords.
- Expanded Git/Docker exclusions to include *.env, .env variants (keeping .env.example), SQL backups and private-key/container certificate files.
- Dev/prod configuration is separated. Production requires explicit hosts and secret, secure cookies, HTTPS redirect, verified database certificate, and disallows SQLite.
- Django check --deploy --fail-level WARNING passed using disposable production placeholders (no live DB connection).
- pnpm audit --prod and pip-audit against the complete pinned Python lockfile reported no known vulnerabilities on this review date. This does not constitute a penetration test.

## 6. Build, CI and deployment
- Build scripts and GitHub Actions exist. Added frontend tests and pip check to CI.
- Local equivalents of build, backend tests, static collection and migration check passed. GitHub Actions itself has not run because no repository is connected.
- Waitress ran the browser test build with DEBUG=False. A separate strict APP_ENV=production server returned the expected HTTP-to-HTTPS 301.
- Full strict-production application/database operation is NOT verified: no hosted SQL database with a trusted certificate/identity exists here.
- Docker is unavailable. Image build, non-root Linux execution, container startup gate and host health-check behavior remain unverified.

## Release blockers and decisions
1. Choose the source repository and hosting platform; provide live domain, organisation details and email-provider configuration via secret settings.
2. Approve or decline restricting new accounts to secretaries. Current code still offers member/auditor roles; no role or account data was silently changed.
3. Import-preview validation fix was explicitly approved and implemented.
4. Build/run the container in CI or a Docker-enabled environment and run deployment_check against the real staging database.
5. Verify hosted HTTPS, cookies, login/recovery email, downloads/imports and restricted database permissions.
6. Decide backup retention/recovery objectives and perform a documented restore drill before go-live.

Changed source files: README.md, setup.ps1, package.json, .github/workflows/verify.yml, .gitignore, .dockerignore, backend/ledger/test_release.py, tests/api.test.mjs. The only runtime behavior change is the approved import-preview validation fix in backend/ledger/views.py. Existing database records were unchanged.
