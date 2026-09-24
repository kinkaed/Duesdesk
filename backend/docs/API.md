# HTTP API

All business endpoints require a Django session. Unsafe operations require an `X-CSRFToken` matching the CSRF cookie. Sign in through `/login/`; there is no public registration. Member/auditor accounts cannot invoke mutation endpoints. Member reads are scoped by server-side querysets, including receipts and exports.

| Method | URL | Purpose |
|---|---|---|
| GET | `/api/session/` | Current user, role, organisation and local date; issues CSRF cookie |
| GET | `/api/overview/?month=2026-09` | Monthly metrics and members |
| GET/POST | `/api/members/` | List/create members |
| GET/POST | `/api/members/1/` | Profile / edit member |
| POST | `/api/payments/preview/` | Preview the automatic allocation |
| GET/POST | `/api/payments/?page=1` | Paginated ledger / record payment |
| POST | `/api/payments/1/void/` | Void with a reason |
| GET | `/receipts/1/` | Printable receipt |
| GET | `/export/?month=2026-09&kind=balances` | CSV balances (or kind=payments) |
| GET | `/export/excel/?month=2026-09&kind=balances` | Excel report |
| GET/POST | `/api/settings/` | Secretary organisation settings |
| GET/POST | `/api/accounts/` | Secretary account management |
| POST | `/api/accounts/1/disable/` | Disable another account |
| GET | `/api/audit/?page=1` | Secretary/auditor audit history |
| GET | `/api/import/template/?kind=members` | Members/payments CSV template |
| POST multipart | `/api/import/preview/` | kind and file, signed ten-minute preview token |
| POST | `/api/import/commit/` | token, transactionally commit reviewed batch |
| GET | `/health/` | Minimal database liveness status |

New payment JSON:

```json
{"member_id":1,"amount":"100.00","start_month":"2026-09","payment_date":"2026-09-22","method":"Cash","reference":"","notes":"","request_key":"c053c911-8736-4563-9727-f636720a9775"}
```

Generate one UUID per intended payment; reuse it on a network retry of the same payload. A changed payload with an already-used key is rejected. This prevents accidental duplicate submission, not duplicates entered intentionally as a new transaction. Methods: Cash, Mobile Money, Bank Transfer, Check. Amount: positive, at most two decimals, maximum GH₵3,000 per receipt. The starting month cannot predate joining, and allocations cannot exceed the last billable month.

Errors are JSON with an `error` message (400 validation, 401 no session, 403 forbidden, 409 conflicting data). Unknown or out-of-scope objects return 404. Some Django 403/404 responses are HTML; the React client handles them without exposing server details.
