# Design record: external identity provider removed

**Status: resolved by removal. Supersedes the earlier "F1 design note: Google account
linking", which is deleted rather than kept as a live proposal.**

## What F1 was

The audit found that Duesdesk would let someone present an external identity and
silently convert an existing account into one that could only be reached that way.
Two consequences, both confirmed by driving the real callback with a mocked provider
response rather than by reading the code alone:

- **Silent conversion.** The account was matched on `email__iexact`, so anyone who
  controlled the external account got the victim's existing secretary or auditor
  session. The only signal was a `google.account_linked` event, which the account
  owner never saw because no notification existed.
- **Permanent lock-in.** Once linked, a different external identity was refused, and
  nothing in the product could unlink or replace one. The account's original
  credential was gone.

A design note laid out six policy options and recommended requiring an authenticated
session before linking, combined with allowing automatic linking only where the
account had no usable password. Every option assumed the provider would still be
there.

## Why the provider was removed instead

An external identity provider is a second identity system inside an application that
already has one: passwords, an emailed verification code, and password recovery. The
provider also introduced the findings the audit could not close while it existed. So
rather than design a linking policy for it, the provider was removed, and:

- every authentication path is now one system, which can be reasoned about as a whole;
- there is no account that can be silently converted, because no external identity
  can be presented at all;
- there is no permanent lock-in, because every account can reach a password.

## What replaced the accounts that had no password

An account created by the provider had no usable password, so Django would not send it
a reset link. Removing the provider without a plan would have locked every one of
those people out of their own organization, which is a worse outcome than either
finding above.

`google_auth.LegacyIdentity` is that plan. Migration `0006_record_legacy_identities`
writes one row per affected account, and only per affected account: active, no
usable password, carrying a provider identity. `AccountRecoveryForm` offers those
accounts a reset link, and the row is deleted the moment a password is set, so the
grant is spent once and the account is ordinary from then on. A disabled account is
never marked and never granted a link.

The provider's own tables are left in place. Nothing is deleted, so an operator can
still inspect what was there and the migration is reversible.

`AUTHENTICATION.md` describes the current flow and the recovery grant. The behaviour
is covered by `backend/google_auth/test_legacy_identity.py` and
`backend/google_auth/test_migrations.py`.

## What is deliberately not decided here

Whether to add an identity provider later is a product decision, and if one is added
it should not reintroduce the silent conversion described above. The invariants are
now in the audit trail regardless of how a session is established: a refusal that
resolves to a real account is written both tenantlessly and into that account's own
organization history.