"""The one place signup creates anything, and the signup rate limit.

Signup creates a usable account immediately. Email ownership is not established
at registration; password recovery is the account recovery mechanism. That is a
deliberate tradeoff, recorded in AUTHENTICATION.md, and a better non-blocking
verification is planned for a later phase.

Because nothing defers account creation any more, this module is the single write
path: the user, the email claim, the organization (or invitation join) and the
membership all commit together or not at all, and the sign-in happens only after
that commit. A failure can never leave a user without an organization, an
organization without a secretary, or an invitation consumed by nobody.

The rate limit here is throughput control, never the uniqueness rule. What makes
an address unique is ``EmailClaim``'s unique constraint: the form's check is
check-then-act, so two concurrent requests can both pass it, and the database is
the final authority. The loser of that race is told the address is already in
use, exactly as the friendly path would have said.
"""
import hashlib
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model, login
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.models import Organisation, SecretaryInvite, UserAccess
from ledger.services import audit as ledger_audit

from .models import EmailClaim, SignupAttempt

logger = logging.getLogger('ledger.auth')

BACKEND = 'django.contrib.auth.backends.ModelBackend'

# Shown when the address already belongs to an account. This is the same wording
# the form uses, so the friendly refusal and a lost race read identically and a
# visitor cannot tell which one they hit.
ALREADY_EXISTS = 'An account with this email already exists, please log in.'
USERNAME_TAKEN = 'That username is already taken. Please choose another.'
INVITE_INVALID = 'This invitation is no longer valid.'
INVITE_EMAIL = 'Use the email address named in your invitation.'
ORG_NAME = 'Enter an organization name.'
RATE_LIMITED = 'Too many sign-up attempts from here. Please try again in a little while.'

# Sliding-hour allowances, deliberately in separate buckets. One address being
# hammered must not stop a different address signing up from the same network,
# and one busy network must not lock out a specific address.
PER_EMAIL_PER_HOUR = 3
PER_IP_PER_HOUR = 5


class Invalid(Exception):
    """The account cannot be created; the message is shown to the secretary."""


def clean_email(value):
    """Normalized exactly the way SignupForm.clean_email() normalizes."""
    return (value or '').strip().lower()


def invite_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest() if raw else ''


# ---------------------------------------------------------------- rate limit

def rate_limited(email, ip):
    """True when this address or this client has spent its hourly allowance.

    Counted from SignupAttempt rows rather than a cache, because the count has to
    survive a restart and hold across the workers. An empty email or a request
    whose REMOTE_ADDR is not a valid address simply does not count against that
    bucket; the other bucket still applies.
    """
    window = timezone.now() - timedelta(hours=1)
    attempts = SignupAttempt.objects.filter(created_at__gte=window)
    email = clean_email(email)
    if email and attempts.filter(email=email).count() >= PER_EMAIL_PER_HOUR:
        return True
    if ip and attempts.filter(ip=ip).count() >= PER_IP_PER_HOUR:
        return True
    return False


def record_attempt(email, ip):
    """Record one account-creation attempt and age out history nobody reads.

    Only the last hour is ever queried, so rows older than that are removed here.
    Signup is low-volume enough that doing it on write keeps the table bounded
    without a scheduled job.
    """
    SignupAttempt.objects.create(email=clean_email(email), ip=ip or '')
    SignupAttempt.objects.filter(created_at__lt=timezone.now() - timedelta(hours=1)).delete()


# ---------------------------------------------------------------- creation

def create(request, *, username, email, password, organization_name, invite_token=''):
    """Create the account, the organization (or join) and the membership.

    All writes are one transaction. The caller has already validated the form;
    this function re-derives nothing from the browser that it does not have to,
    and it is the only signup path that writes.

    Raises :class:`Invalid` with a secretary-facing message. On success the
    account is signed in and returned.
    """
    email = clean_email(email)
    organization_name = (organization_name or '').strip()

    try:
        with transaction.atomic():
            invite = None
            if invite_token:
                # Serialize invitations and memberships on the organization row.
                invite = (SecretaryInvite.objects.select_for_update()
                          .filter(token_hash=invite_hash(invite_token),
                                  used_at__isnull=True, revoked_at__isnull=True,
                                  expires_at__gt=timezone.now())
                          .select_related('organization').first())
                if invite is None or invite.organization.disabled_at:
                    raise Invalid(INVITE_INVALID)
                if invite.email.casefold() != email.casefold():
                    raise Invalid(INVITE_EMAIL)
                org = Organisation.objects.select_for_update().get(pk=invite.organization_id)
            else:
                if not organization_name:
                    raise Invalid(ORG_NAME)
                # The palette stays at its defaults until the branding step; only
                # the name is chosen here.
                org = Organisation(name=organization_name, onboarding_required=True)
                try:
                    org.full_clean(exclude=['public_id'])
                except ValidationError as error:
                    raise Invalid(_first_message(error)) from error
                org.save()

            user = get_user_model()(username=username, email=email)
            # Assigning through set_password() hashes once. Nothing retains the
            # submitted password after this line.
            user.set_password(password)
            try:
                user.full_clean(exclude=['password'])
            except ValidationError as error:
                raise Invalid(USERNAME_TAKEN) from error
            user.save()

            # The database authority. A concurrent signup for the same address
            # fails this INSERT, which rolls the whole block back.
            EmailClaim.objects.create(email=email, user=user)

            UserAccess.objects.create(user=user, organization=org, role='secretary')
            if invite:
                invite.used_at = timezone.now()
                invite.used_by = user
                invite.save(update_fields=['used_at', 'used_by'])

            ledger_audit(user, 'organization.joined' if invite else 'organization.created', org)
    except IntegrityError:
        # A unique constraint was violated between the form's check and this
        # insert: either the username or the email claim. The transaction has
        # already rolled back, so nothing partial survives. Which one it was is
        # decided by looking, not by parsing the driver's message.
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise Invalid(USERNAME_TAKEN)
        raise Invalid(ALREADY_EXISTS)

    # Only once the transaction has committed. Signing in before the commit would
    # leave a live session for an account that does not exist.
    login(request, user, backend=BACKEND)
    return user


def _first_message(error):
    return error.messages[0] if error.messages else 'Enter valid details.'


