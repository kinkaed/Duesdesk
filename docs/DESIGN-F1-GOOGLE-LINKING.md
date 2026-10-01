# F1 design note: Google account linking

**Status:** DECISION REQUIRED — implementation deliberately not started
**Date:** 2026-09-30 · **Audited commit:** `1f01894` · **Current HEAD:** `d8b5c58`

## The invariant being violated

> A new external identity must not acquire an existing account's privileges solely
> because the provider-reported email matches the existing account.

## What the code does today

`adapters.py:150-210` (`sign_in`) is the only path that can link a Google identity to an
existing account. Its sequence is:

1. `validate_claims` rejects anything with an unverified email claim (`adapters.py:87,92-93`).
2. `filter(email__iexact=email)` resolves the account (`adapters.py:162`). Exactly one match
   is required; zero is `no matching account`, two is `ambiguous email`.
3. `UserAccess` is resolved and must be active with a valid role (`adapters.py:167-169`).
4. If no `SocialAccount` exists for this `uid`, one is created (`adapters.py:179-188`).
5. The email is marked verified and primary (`adapters.py:189-194`).
6. `login()` runs and the session begins (`adapters.py:207`).

So the *only* thing establishing that the caller may act as the account is that Google
reported a matching address. Nothing asks the account's owner.

### What is already correct

Worth stating, because it constrains the fix:

- `email_verified` is checked server-side (`adapters.py:87`) against a value from Google's
  server-to-server token exchange, not from the browser.
- State is session-bound, single-use, expiring, and PKCE is on. The callback cannot be
  turned into a signup (`views.py:69-72`).
- One Google identity per account (`adapters.py:183-184`), and an address cannot be linked to
  two users (`adapters.py:175-176`). Row locks serialise a concurrent first link.
- Nothing is ever created in `sign_in`. An unknown address is a refusal, never a signup.
- `authenticate_by_email` returns `None` (`adapters.py:58-61`), so allauth's own
  address-matching grant is already disabled.

### The two consequences that matter

**a. Silent conversion.** Anyone holding the victim's Google account gets the victim's
secretary/auditor session, and the account becomes Google-linked as a side effect.

**b. Irreversible lock-out.** After a first link, `adapters.py:183-184` refuses a *different*
Google identity for that account. The legitimate owner is locked out of Google sign-in
permanently and can only return via password reset. If the account was created by Google
signup and has no usable password, recovery is a manual support action.

Note the asymmetry: `email_verified` proves the caller controls *a Google account at that
address*. It does not prove the caller is the Duesdesk account holder.

## Options considered

| # | Policy | Closes (a) | Closes (b) | Cost | Verdict |
|---|---|---|---|---|---|
| 1 | Require an already-authenticated session before linking | Yes | No | Small | Rejected alone: does not address lock-out |
| 2 | Require password confirmation before linking | Yes | Partly | Medium | Rejected alone: unusable for Google-only accounts |
| 3 | Separate link-confirmation step (email a one-time code to the account's address) | Yes | No | Medium | Viable, but adds a flow and still locks the original identity in |
| 4 | Allow automatic linking **only** when the account has no usable password | No | Yes | Small | Rejected alone: leaves the silent conversion |
| 5 | **1 + 4 combined** | Yes | Yes | Small–medium | **Recommended** |
| 6 | Never link automatically; require an explicit in-product "connect Google" action | Yes | Yes | Large | Rejected as disproportionate now |

## Recommendation: option 5

- **Account already has a usable password** → require an authenticated session (password login
  or an existing Google session for that same account) before linking. The existing
  `adapters.py:170-171` session-mismatch check already refuses a signed-in *different* user;
  this tightens it to require the *same* user be signed in.
- **Account has no usable password** (Google-signup account) → continue linking automatically.
  There is no second factor to check, so demanding one would lock those users out of their own
  account for no security gain.
- **Either way**, `google.account_linked` fires, and the owner is notified by email.

The no-password test is the honest discriminator here: it is the only condition under which
email matching is the *sole* credential the account has.

## Cases this policy must cover

| Case | Required outcome |
|---|---|
| New Google signup | Unchanged. Must keep working. |
| Google-linked account, returns later | Unchanged. No re-link, no second event. |
| Password account + matching verified Google email, not signed in | **Refuse**, with a message telling them to sign in first |
| Password account + matching verified Google email, signed in as that user | Link, notify |
| Different Google identity, same address, account already linked | Already refused (`adapters.py:183-184`) |
| Unverified Google email | Already refused (`adapters.py:92-93`) |
| Signed in as a *different* account, matching email | Already refused (`adapters.py:170-171`) |

## Notification

The existing mail infrastructure (`django.core.mail`, already used for signup verification and
recovery) is sufficient. The message states that a Google account was connected, the time, and
that it can be reported if unrecognised. It must contain no OAuth token, no provider payload, and
no session identifier. This is genuinely useful rather than decorative: it is the only signal the
legitimate owner of a silent conversion would ever receive.

## Why this is not being implemented in this pass

Two things are genuinely product decisions, not implementation details, and guessing at them
would be worse than asking:

1. **Whether passwordless (Google-only) accounts may be auto-linked.** Option 4 accepts this.
   If the answer is no, every Google-only user needs a recovery path before the policy can
   tighten, which is a larger change.
2. **What the user sees.** Refusing a link means changing the sign-in page copy and adding an
   "unrecognised? sign in with your password instead" path. That is a UX change to a page the
   task also asks to keep unchanged.

Related: there is currently **no way to unlink or replace a Google identity** — no
`socialaccount` disconnect view is mounted (`config/urls.py:14-20`) and nothing deletes a
`SocialAccount`. That is what makes consequence (b) permanent. Adding an unlink capability is
independent of the policy above and is worth doing regardless, but it is a product surface
(where does it live in the UI?) rather than a security fix.

## Test plan once decided

`google_auth/tests.py` already covers new signup, unknown address, already-linked identity, a
linked identity claiming another address, and audit content. To add:

- password account + matching Google + **not** signed in → refused, no `SocialAccount` created
- password account + matching Google + signed in as that user → linked, audit event, notified
- Google-only account + matching Google → still links (locks in the policy)
- link failure leaves no partial `SocialAccount` / verified `EmailAddress`
- the notification email contains no token or secret