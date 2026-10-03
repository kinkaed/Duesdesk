# Design record: signup verification removed, redesigned later

**Status: decided and implemented (verification removed now). The replacement design
below is deliberately deferred and must not be started without a new decision.**

## What signup used to be

Submitting the signup form stored a `PendingSignup`: no user, no organization and no
membership existed until a six-digit code emailed to the address was entered. The code
expired after ten minutes, wrong attempts were counted on the pending row, and
completion was one transaction that created the account and consumed the row.

Verification was the gate, so the account did not exist until the address had been
proven.

## Why it was removed

Three reasons, in order of weight.

1. **It did not actually prove what it claimed.** The pending row held the password
   hash and the branding the person had typed. Anyone who could read the pending row
   could see that data, and the address was only proven *after* it had been collected.
   The gate protected the mailbox, not the person.
2. **It made delivery a hard dependency of having an account at all.** On the free
   Render tier outbound SMTP is blocked, so a signup could fail entirely through no
   fault of the person signing up. A verification step that cannot send is worse than
   no verification step: it strands a valid account behind infrastructure the operator
   has not authorized yet.
3. **It carried its own attack surface.** Session-bound pending handles, code hashing
   across deploys, resend budgets and stale-object handling were all complexity that
   existed only to support the gate. Removing the gate removes all of it.

The external-identity removal (`docs/DESIGN-EXTERNAL-IDENTITY-REMOVAL.md`) removed one
second identity system inside the application. Immediate creation removes the second
half of the same problem: a partially-created account type with its own lifecycle.

## What signup guarantees now

- **One address, one account, enforced by the database.** `google_auth.EmailClaim` is
  a unique row keyed by the stripped, lowercased address. The form's case-insensitive
  check gives a friendly refusal; the unique constraint is what makes the rule true
  when two requests race. A lost race gets the same message as a duplicate submitted a
  second later.
- **No account is half-created.** User, organization, secretary membership and the
  `EmailClaim` are written in one transaction. The person is signed in only after that
  transaction commits.
- **Refusals do not enumerate.** A duplicate address, an invalid invitation and a
  rate-limited request all render the same page. Rate-limited requests are not
  recorded, so the limiter cannot be used to test which addresses exist.
- **Password recovery is the recovery mechanism.** It is what eventually proves the
  address, because the reset link can only be read by whoever controls the mailbox.

## The risk this accepts

An attacker can register an address they do not control. They cannot use it to enter an
existing account: `EmailClaim` and the case-insensitive check refuse an address that
already has one, so there is no takeover path. What they get is an empty organization
under an address they cannot read mail for. Two consequences follow.

- **Squatting.** When the real owner of the address later signs up they are refused,
  and the way in is password recovery. Because the recovery mail goes to the mailbox
  they control, they end up setting a password on the squatter's account and take the
  empty workspace. The squatter is locked out. This is the intended resolution, and it
  is only clean because the organization was empty.
- **Data left behind.** If a squatter populated the organization before the owner
  recovered it, the owner would inherit those records. Nothing prevents this today; it
  is the reason verification is a planned improvement rather than a permanently
  rejected one.

This is accepted for the current product: secretary signup is self-service, there is no
invite gate, and a brand-new organization is empty by construction. It is recorded here
so the tradeoff is not rediscovered as a bug.

## The deferred design: non-blocking verification

The replacement must be **non-blocking**. The account is created and usable
immediately, exactly as it is now, and verification adds a proven-address flag rather
than a gate.

- **Where the flag lives.** A nullable `verified_at` on `EmailClaim`. A cast no longer
  needs `PendingSignup` under another name; the claim already owns the address and is
  the right row to record when it was proven. A new model would reintroduce the second
  lifecycle this change removed.
- **What sends it.** Creation triggers one verification email through the existing mail
  path. Delivery failure is logged and never rolls back the account. A resend endpoint
  is rate limited per claim, reusing the `SignupAttempt` pattern.
- **What it unlocks.** Only things that genuinely need a proven address should wait for
  it: unattended password reset (as opposed to reset from an active session), email
  notifications, and inviting other secretaries. Everything the product does today
  should keep working unverified, or this becomes a gate with extra steps.
- **Legacy and backfill.** Existing `EmailClaim` rows are migration-marked verified, so
  a deploy does not silently un-verify working accounts. New rows start unverified.
- **Visibility.** An unverified workspace shows the state in the interface and offers
  "resend". The state is advisory, never a lock on the person's own data.

None of this is implemented. It is written down so the next phase starts from a
decision rather than re-deriving one, and so the accepted risk above has a planned exit.

## What this record does not decide

- Whether an unverified address may invite another secretary.
- Whether verification is required to create a *second* organization.
- How to treat an organization populated by a squatter after an owner recovers the
  address (transfer, archive, or leave as-is).
- The mail transport the verification send would use.

Each of these needs its own decision and its own test.
