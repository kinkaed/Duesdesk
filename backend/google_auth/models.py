"""Account-creation records: the email uniqueness authority and the rate limit.

Signup creates a usable account immediately. Email ownership is not established
at registration; password recovery is the account recovery mechanism. That is a
deliberate tradeoff, recorded in AUTHENTICATION.md, and a better non-blocking
verification is planned for a later phase.

Two small tables support that decision:

* ``EmailClaim`` is the database authority for one account per address. Django's
  ``User.email`` is not unique and the signup form's check is check-then-act, so
  without this two concurrent requests could both create an account for the same
  email.
* ``SignupAttempt`` records account-creation requests for the sliding-hour rate
  limit, since verification is gone and the only other signup throttle went with
  it.

Neither table carries an organization: an account-creation request has no tenant
yet, and inventing one would be a lie.
"""
import secrets

from django.conf import settings
from django.db import models


def new_token():
    """A fresh unguessable value.

    Retained only because the historical PendingSignup migration (0003)
    serialises this function as a field default, so removing it would make an
    already-applied migration unimportable. Nothing in the current flow calls it.
    """
    return secrets.token_urlsafe(32)


class EmailClaim(models.Model):
    """The database authority that one address belongs to one account.

    ``email`` is stored stripped and lowercased. The signup form still performs a
    friendly uniqueness check, but this row is what makes the rule true under
    concurrency: two simultaneous signups for one address cannot both commit,
    because the second INSERT violates the unique constraint.

    There is no email-change flow. If one is ever added it must move this claim in
    the same transaction as the User update, or the two would disagree.
    """

    email = models.EmailField(max_length=254, unique=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='email_claim')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Email claim'

    def __str__(self):
        return self.email


class SignupAttempt(models.Model):
    """One account-creation request, for the signup rate limit.

    Tenantless, like ``AuthRejection``: the request has no account yet, so there
    is no organization to file it under. Attempts are counted over a sliding
    hour, separately per normalized email and per client IP. The IP is whatever
    ``ledger.observability.client_ip`` resolved from ``REMOTE_ADDR``; a forwarded
    header is never read.

    This is a throughput control only. It is never the thing that makes an email
    unique -- that is ``EmailClaim``.
    """

    # Normalized exactly as SignupForm.clean_email(): stripped and lowercased.
    email = models.CharField(max_length=254, blank=True, default='')
    ip = models.CharField(max_length=45, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-id']
        verbose_name = 'Signup attempt'
        indexes = [
            models.Index(fields=['email', '-created_at'], name='signup_attempt_email'),
            models.Index(fields=['ip', '-created_at'], name='signup_attempt_ip'),
        ]


class AuthRejection(models.Model):
    """A refused authentication event that no organization can own.

    ``ledger.AuditEvent`` requires an organization, and a rejected attempt by
    someone with no active membership has no organization and must never be
    given one. Those attempts are recorded here instead. This table is
    intentionally global: it carries no tenant column, no tenant foreign key and
    no tenant scoping, and nothing in the application reads it to authorize
    anything.

    Signup no longer verifies an email address, so the verification refusals that
    used to dominate this table are gone; what remains are refusals that resolve
    to no membership. ``flow`` still says which entry point the browser had
    started, and ``method`` is retained for historical rows.

    Rows hold the address involved, which is attacker-influenceable and may
    belong to a person who has no account here. Treat the table as confidential
    operational data.
    """

    action = models.CharField(max_length=60)
    reason = models.CharField(max_length=60)
    flow = models.CharField(max_length=16, blank=True, default='')
    method = models.CharField(max_length=8, blank=True, default='')
    # The address being verified; blank when the attempt was unusable.
    email = models.CharField(max_length=254, blank=True, default='')
    # Populated only when the attempt resolved to an account this server owns.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name='+')
    # REMOTE_ADDR as seen by the application server. Never a forwarded header.
    ip = models.CharField(max_length=45, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-id']
        verbose_name = 'Authentication rejection'


class LegacyIdentity(models.Model):
    """An account that arrived through an external provider and owes a password.

    Duesdesk no longer authenticates against any external provider. Accounts that
    were created by one never had a usable password, and Django's password reset
    refuses to serve exactly those accounts -- by design, since an address that
    was never proven to *this* server must not be able to claim the account.

    When the external provider was removed, the identity table said which
    addresses had been proven there, and the migration recorded one row per such
    account here. That row is the evidence that the address was verified while
    the provider existed, and it is what lets the reset form offer a password to
    an account that otherwise has none.

    This grants nothing by itself. The user still has to prove control of the
    mailbox, and the flag is cleared the moment they do, so the grant is spent
    once. Nothing else in the application reads this table.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='legacy_identity')
    # The provider that created the account, kept only as provenance for whoever
    # is auditing the removal. It is a historical string, not a live integration.
    provider = models.CharField(max_length=32)
    # The address that provider verified, kept as evidence alongside the flag.
    email = models.CharField(max_length=254, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = 'Legacy identity'

    def __str__(self):
        return f'LegacyIdentity {self.user_id} via {self.provider}'
