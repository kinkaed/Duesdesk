"""The verification half of signup: prove an address, then create the account.

Creating the user and the organization is the irreversible part of signup, so
it happens in exactly one place and only after a verification has succeeded.
Everything before that point creates nothing: a ``PendingSignup`` row and, if
asked, an email. No user, no organization, no membership, no invitation change.

The existing secretary-only signup rules are reused rather than reimplemented,
so the invitation checks, the branding fields and the "organization first"
ordering behave exactly as they did before verification existed.
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model, login
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount

from ledger.branding import DEFAULTS, color, read_logo_token
from ledger.models import Organisation, SecretaryInvite, UserAccess
from ledger.services import audit as ledger_audit

from . import flows
from .audit import record, record_user
from .models import (CODE_ATTEMPTS, CODE_LIFETIME, GOOGLE, PENDING_LIFETIME,
                     RESEND_COOLDOWN as COOLDOWN, SENDS_PER_HOUR, PendingSignup,
                     new_token)

logger = logging.getLogger('ledger.google_auth')

BACKEND = 'django.contrib.auth.backends.ModelBackend'
ALREADY_EXISTS = 'An account with this email already exists, please log in.'
GOOGLE_MISMATCH = 'The Google account email does not match the email you signed up with.'
# The session key holding a Google uid that has just proven a pending address.
GOOGLE_UID = 'pending_google_uid'

VERIFY_URL = '/signup/verify/'

# Refusals, shown on the verification page.
MESSAGES = {
    'google_mismatch': GOOGLE_MISMATCH,
    'unverified_email': 'Google has not verified that email address. Please try again.',
    'wrong_code': 'That code is not correct. Please check and try again.',
    'expired_code': 'That code has expired. Please request a new one.',
    'too_many_attempts': 'Too many incorrect attempts. Please request a new code.',
    'no_code': 'Enter the 6-digit code we emailed you.',
    'cooldown': 'Please wait a moment before asking for another code.',
    'hourly_cap': 'Too many codes requested. Please try again later.',
    'no_pending': 'This sign-up has expired. Please sign up again.',
    'code_sent': 'We emailed you a 6-digit code.',
    'invite_invalid': 'This invitation is no longer valid.',
    'invite_email': 'Use the email address named in your invitation.',
    'org_name': 'Enter an organization name.',
    'username_taken': 'That username is already taken. Please choose another.',
    'color': 'Colors must be six-digit hex values, such as #214f43.',
}


def message(request, key):
    """Show a stored message once, then forget it."""
    return request.session.pop(flows.SIGNUP_ERROR, None) or MESSAGES.get(key, '')


# ---------------------------------------------------------------- the record

def create(request, form, organization_name, invite_token='', palette=None, logo_token=''):
    """Store a validated signup for later, or explain why it cannot be stored.

    Returns ``(pending, None)`` on success, or ``(None, message)``. Nothing
    here creates a user or an organization.
    """
    email = flows.clean(form.cleaned_data['email'])
    if get_user_model().objects.filter(email__iexact=email).exists():
        return None, ALREADY_EXISTS
    if get_user_model().objects.filter(username__iexact=form.cleaned_data['username']).exists():
        return None, MESSAGES['username_taken']

    logo = None
    if logo_token:
        try:
            logo = read_logo_token(logo_token, preview_owner(request))
        except ValueError as error:
            return None, str(error)

    pending = PendingSignup(
        email=email,
        username=form.cleaned_data['username'],
        organization_name=organization_name,
        # A hash. The submitted password is not retained anywhere.
        password_hash=make_password(form.cleaned_data['password1']),
        palette=dict(palette or {}),
        logo=logo,
        # Only the hash of an invitation token, so an invite-driven signup can
        # be completed later without a usable token sitting in the database.
        invite_token_hash=invite_hash(invite_token),
        token=new_token(),
        expires_at=timezone.now() + timedelta(seconds=PENDING_LIFETIME),
    )
    try:
        with transaction.atomic():
            # Latest submission wins, so two pending signups can never compete
            # for one address.
            PendingSignup.objects.filter(email=email).delete()
            pending.save()
    except IntegrityError:
        logger.exception('Could not store a pending signup for %s', email)
        return None, 'We could not start your sign-up. Please try again.'
    return pending, None


def preview_owner(request):
    if not request.session.session_key:
        request.session.create()
    return f'session:{request.session.session_key}'


def invite_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest() if raw else ''


def current(request):
    """The pending signup this session is verifying, if it is still usable.

    Reached only through the unguessable token in the session, so a visitor
    cannot enumerate pending signups by id or by email.
    """
    token = request.session.get(flows.PENDING_TOKEN)
    if not token:
        return None
    pending = PendingSignup.objects.filter(token=token).first()
    if pending is None or pending.expired():
        return None
    return pending


# ---------------------------------------------------------------- the code

def send_code(request, pending):
    """Email a fresh 6-digit code, subject to the cooldown and the hourly cap."""
    now = timezone.now()
    stamps = sorted(t for t in map(parse_datetime, pending.code_sends or []) if t)
    if stamps and (now - stamps[-1]).total_seconds() < COOLDOWN:
        return MESSAGES['cooldown']
    if len([t for t in stamps if now - t < timedelta(hours=1)]) >= SENDS_PER_HOUR:
        return MESSAGES['hourly_cap']

    digits = f'{secrets.randbelow(1000000):06d}'
    pending.code_hash = make_code_hash(pending, digits)
    pending.code_expires_at = now + timedelta(seconds=CODE_LIFETIME)
    pending.code_attempts = 0
    pending.code_dead = False
    # Keep only the send times still relevant to the sliding-hour cap.
    pending.code_sends = [t.isoformat() for t in stamps if now - t < timedelta(hours=1)] \
        + [now.isoformat()]
    pending.save(update_fields=['code_hash', 'code_expires_at', 'code_attempts',
                                'code_dead', 'code_sends'])
    send_mail(
        subject='Your Duesdesk verification code',
        message=f'Your Duesdesk verification code is {digits}.\n\n'
                'It expires in 10 minutes. If you did not request it, ignore this email.',
        from_email=None,
        recipient_list=[pending.email],
        fail_silently=True,
    )
    return ''


def make_code_hash(pending, digits):
    """PBKDF2 over the code, salted with the pending signup's own token.

    Binding the hash to the row means a stolen database is not enough to try a
    guessed code against another row, and the digits are never stored.
    """
    return make_password(f'{pending.token}${digits}')


def check_code(request, pending, digits):
    """Try a code. Returns '' when it is right, else the message to show."""
    digits = (digits or '').strip()
    if not digits:
        return MESSAGES['no_code']
    if pending.code_dead or pending.code_attempts >= CODE_ATTEMPTS:
        return MESSAGES['too_many_attempts']
    if not pending.code_hash:
        return MESSAGES['no_code']
    if not pending.code_expires_at or pending.code_expires_at <= timezone.now():
        return MESSAGES['expired_code']

    if check_password(f'{pending.token}${digits}', pending.code_hash):
        # Single use: spent whether or not the account creation that follows
        # happens to succeed.
        pending.code_hash = ''
        pending.code_dead = True
        pending.save(update_fields=['code_hash', 'code_dead'])
        return ''

    # Counted after the check, so a correct guess is never penalised, and the
    # value is never written anywhere.
    pending.code_attempts += 1
    if pending.code_attempts >= CODE_ATTEMPTS:
        pending.code_dead = True
        pending.code_hash = ''
    pending.save(update_fields=['code_attempts', 'code_dead', 'code_hash'])
    return MESSAGES['too_many_attempts'] if pending.code_dead else MESSAGES['wrong_code']


# ---------------------------------------------------------------- completion

class AlreadyVerified(Exception):
    """This pending signup was already completed and removed."""


class Invalid(Exception):
    """The signup cannot be completed; the message is shown to the secretary."""


def complete(request, pending, method):
    """Create the account, the organization and the membership, all or nothing.

    The only place signup creates anything. Every write is inside one
    transaction, so a failure can never leave a user without an organization,
    an organization without a secretary, or an invitation consumed by nobody.
    """
    with transaction.atomic():
        # Serialize on the pending row so two concurrent verifications of the
        # same signup cannot both create a user.
        locked = PendingSignup.objects.select_for_update().filter(pk=pending.pk).first()
        if locked is None:
            raise AlreadyVerified()

        invite = None
        if locked.invite_token_hash:
            # Serialize invitations and memberships on the organization row.
            invite = (SecretaryInvite.objects.select_for_update()
                      .filter(token_hash=locked.invite_token_hash, used_at__isnull=True,
                              revoked_at__isnull=True, expires_at__gt=timezone.now())
                      .select_related('organization').first())
            if invite is None:
                raise Invalid(MESSAGES['invite_invalid'])
            if invite.email.casefold() != locked.email.casefold():
                raise Invalid(MESSAGES['invite_email'])
            org = Organisation.objects.select_for_update().get(pk=invite.organization_id)
        else:
            if not locked.organization_name:
                raise Invalid(MESSAGES['org_name'])
            org = Organisation(name=locked.organization_name)
            for field, default in DEFAULTS.items():
                try:
                    setattr(org, field, color((locked.palette or {}).get(field, default)))
                except ValueError as error:
                    raise Invalid(str(error)) from error
            if locked.logo:
                org.logo = locked.logo
            org.full_clean()
            org.save()

        user = get_user_model()()
        user.username = locked.username
        user.email = locked.email
        # The hash was made from the submitted password. Assigning it directly
        # keeps that password; hashing it again would invalidate it.
        user.password = locked.password_hash
        try:
            user.full_clean(exclude=['password'])
        except ValidationError as error:
            raise Invalid(MESSAGES['username_taken']) from error
        user.save()

        UserAccess.objects.create(user=user, organization=org, role='secretary')
        if invite:
            invite.used_at = timezone.now()
            invite.used_by = user
            invite.save(update_fields=['used_at', 'used_by'])

        # The address has just been proven, by whichever method was used.
        # The address itself goes in the defaults as well as the lookup:
        # update_or_create only writes the defaults on create.
        EmailAddress.objects.update_or_create(
            user=user, email__iexact=locked.email,
            defaults={'user': user, 'email': locked.email, 'verified': True,
                      'primary': True})
        if method == GOOGLE:
            _link_google(request, user, locked.email)

        PendingSignup.objects.filter(pk=locked.pk).delete()

        ledger_audit(user, 'organization.joined' if invite else 'organization.created', org)
        record_user(request, 'signup.email_verified', user, method=method, email=locked.email)

    # Only once the transaction has committed. Signing in before the commit
    # would leave a live session for an account that does not exist.
    login(request, user, backend=BACKEND)
    request.session[flows.LOGGED_IN_EMAIL] = locked.email
    request.session.pop(flows.PENDING_TOKEN, None)
    flows.finish(request, flows.SIGNUP)
    return user


def _link_google(request, user, email):
    """Attach the Google identity that just proved this address.

    The uid was checked against the stored identity by the adapter before this
    runs, so this cannot overwrite a link belonging to somebody else.
    """
    uid = request.session.pop(GOOGLE_UID, None)
    if not uid:
        return
    SocialAccount.objects.update_or_create(
        provider='google', uid=uid, user=user,
        defaults={'extra_data': {'email': email, 'email_verified': True}})


def refuse(request, key, pending=None, action='signup.verification_rejected', reason=None, email='',
           method=''):
    """Record a refusal and hand the reason to the verification page."""
    record(request, action, reason=reason or key,
           email=email or (pending.email if pending else ''), flow=flows.SIGNUP,
           method=method)
    request.session[flows.SIGNUP_ERROR] = MESSAGES.get(key, MESSAGES['no_pending'])