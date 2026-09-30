"""Rejection audit for sign-in attempts that no organization can own."""
from django.conf import settings
from django.db import models


class GoogleAuthRejection(models.Model):
    """A refused Google sign-in that has no organization.

    ``ledger.AuditEvent`` requires an organization, and a rejected attempt by
    someone who is not yet a user has no organization and must never be given
    one. Those attempts are recorded here instead. This table is intentionally
    global: it carries no tenant column, no tenant foreign key and no tenant
    scoping, and nothing in the application reads it to authorize anything.

    Rows hold the address Google presented, which is attacker-influenceable and
    may belong to a person who has no account here. Treat the table as
    confidential operational data.
    """

    provider = models.CharField(max_length=32, default='google')
    action = models.CharField(max_length=60)
    reason = models.CharField(max_length=60)
    # The address presented by Google; blank when the claim was missing or unusable.
    email = models.CharField(max_length=254, blank=True, default='')
    # Populated only when the attempt resolved to an account this server owns.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name='+')
    # REMOTE_ADDR as seen by the application server. Never a forwarded header.
    ip = models.CharField(max_length=45, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-id']
        verbose_name = 'Google sign-in rejection'
