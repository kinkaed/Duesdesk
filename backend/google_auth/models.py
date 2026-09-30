"""A signup that exists only until the address is proven.

Creating the account and the organization is the irreversible part of signup:
it produces financial records, an organization row and a membership that must
never be orphaned. So nothing is created until the address has been proven. A
``PendingSignup`` holds the submitted details, including a password hash but
never a plaintext password, and the ordinary secretary-only signup logic runs
only once a verification has succeeded and the whole thing commits or not at
all.

This table has no organization and no user. Until verification completes there
is no tenant to own it, and inventing one would be a lie.
"""
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_datetime

PENDING_LIFETIME = 60 * 60 * 24          # 24 hours
CODE_LIFETIME = 10 * 60                  # 10 minutes
CODE_ATTEMPTS = 5                        # then the code is dead, request a new one
RESEND_COOLDOWN = 60                     # seconds between sends
SENDS_PER_HOUR = 5
GOOGLE = 'google'
CODE = 'code'


def new_token():
    return secrets.token_urlsafe(32)


class PendingSignup(models.Model):
    """A submitted signup waiting to have its email address proven.

    ``email`` is unique, so resubmitting for the same address replaces the
    earlier attempt rather than accumulating duplicates.
    """

    # The address to be proven, always lowercased so a later comparison cannot
    # be defeated by differing case.
    email = models.EmailField(max_length=254, unique=True)
    username = models.CharField(max_length=150)
    organization_name = models.CharField(max_length=120)
    # A hash, produced by make_password(). Never the submitted password.
    password_hash = models.CharField(max_length=256)
    # Organization colors, kept so branding survives the verification detour.
    palette = models.JSONField(default=dict, blank=True)
    # The decoded logo, so it does not expire with the 30-minute preview token.
    logo = models.BinaryField(null=True, blank=True)
    # hash() of the invitation token, so a pending invite-driven signup can be
    # completed against the same invitation. Never the token itself.
    invite_token_hash = models.CharField(max_length=64, blank=True, default='')

    # The unguessable handle used to find this row from the session. The primary
    # key is never used as a handle and the email is never used in a URL.
    token = models.CharField(max_length=64, default=new_token, db_index=True)

    # The emailed code: a PBKDF2 hash, never the digits.
    code_hash = models.CharField(max_length=256, blank=True, default='')
    code_expires_at = models.DateTimeField(null=True, blank=True)
    code_attempts = models.PositiveSmallIntegerField(default=0)
    # Send timestamps, used for the resend cooldown and the hourly cap.
    code_sends = models.JSONField(default=list, blank=True)
    code_dead = models.BooleanField(default=False)

    # A completed signup deletes its row, so this only exists to say the attempt
    # is no longer usable.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-id']
        verbose_name = 'Pending signup'

    def __str__(self):
        return f'PendingSignup {self.email}'

    def expired(self):
        return self.expires_at <= timezone.now()

    def code_live(self):
        """True when a code was sent and can still be entered correctly."""
        return bool(self.code_hash) and not self.code_dead \
            and self.code_expires_at and self.code_expires_at > timezone.now() \
            and self.code_attempts < CODE_ATTEMPTS

    def can_send_code(self):
        """True when the cooldown and the hourly cap both allow another send."""
        now = timezone.now()
        stamps = sorted(t for t in map(parse_datetime, self.code_sends or []) if t)
        if stamps and (now - stamps[-1]).total_seconds() < RESEND_COOLDOWN:
            return False
        # The cap is a sliding hour, so a send from 59 minutes ago still counts.
        return len([t for t in stamps if now - t < timedelta(hours=1)]) < SENDS_PER_HOUR


class GoogleAuthRejection(models.Model):
    """A refused verification or sign-in that no organization can own.

    ``ledger.AuditEvent`` requires an organization, and a rejected attempt by
    someone who is not yet a user has no organization and must never be given
    one. Those attempts are recorded here instead. This table is intentionally
    global: it carries no tenant column, no tenant foreign key and no tenant
    scoping, and nothing in the application reads it to authorize anything.

    ``flow`` says which entry point the browser had started, so a refusal to
    verify a signup address is never confused with a refusal to sign in.
    ``method`` says how a signup address was being proven: 'google' or 'code'.

    Rows hold the address involved, which is attacker-influenceable and may
    belong to a person who has no account here. Treat the table as confidential
    operational data.
    """

    provider = models.CharField(max_length=32, default='google')
    action = models.CharField(max_length=60)
    reason = models.CharField(max_length=60)
    flow = models.CharField(max_length=16, blank=True, default='')
    method = models.CharField(max_length=8, blank=True, default='')
    # The address presented by Google, or the address being verified; blank when
    # the claim was missing or unusable.
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
