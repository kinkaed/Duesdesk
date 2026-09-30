"""The signup half of Google: prove an address, then let signup do its job.

Nothing here creates an account or an organization. The proof is only a
session fact, and the ordinary signup submission refuses to run without it.
The user, the organization, the membership and the invitation are all still
created by the existing secretary-only signup logic.
"""
import logging

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import flows
from .audit import record

logger = logging.getLogger(__name__)

GOOGLE = 'google'
MESSAGES = {
    flows.MISSING: 'Verify this email address with Google before creating your account.',
    flows.MISMATCH: 'Verify this email address with Google before creating your account.',
    flows.EXPIRED: 'Your Google verification has expired. Please verify this email address with Google again.',
}
# Only the failures that mean somebody tried to get in without proving anything
# are recorded. A form that was simply never verified is an ordinary signup
# attempt, not a Google refusal.
REASONS = {
    flows.MISSING: 'missing verification',
    flows.MISMATCH: 'email mismatch',
    flows.EXPIRED: 'expired verification',
    'no identity': 'missing identity claim',
}


def verified_email(request):
    """The address Google has proven this session, or '' while there is none."""
    return flows.verified_email(request)


def block(request, email):
    """Return the reason this signup may not proceed, or an empty string.

    Called from the signup view with the address the form submitted, so the
    check is server-side and cannot be satisfied by anything the browser says.
    """
    verdict = flows.verification(request, email)
    if verdict == flows.OK and not flows.signup_uid(request):
        # The identity is half of what was proven, and it is the half that gets
        # linked to the account. A proof with nothing to link would create an
        # account its owner could never open with Google afterwards.
        verdict = 'no identity'
    if verdict == flows.OK:
        return ''
    if verdict in REASONS:
        record(request, 'google_signup_rejected', None, REASONS[verdict],
               flows.clean(email), flow=flows.SIGNUP)
    return MESSAGES.get(verdict, MESSAGES[flows.MISSING])


def complete(request, user):
    """Record the proof once the account exists.

    Called from inside the signup transaction, after the membership has been
    created, so the audit event can name a real organization.
    """
    email = flows.normalize(user.email)
    if flows.verification(request, email) != flows.OK:
        return
    uid = flows.signup_uid(request)
    address = EmailAddress.objects.filter(user=user, email__iexact=email).first()
    if address is None:
        address = EmailAddress(user=user, email=email)
    address.verified = True
    address.primary = not EmailAddress.objects.filter(user=user, primary=True).exclude(pk=address.pk).exists()
    address.save()
    if uid and not SocialAccount.objects.filter(provider=GOOGLE, user=user).exists():
        # The person proved they own this address, so the identity they just
        # used belongs with the account it was proven for. A concurrent signup
        # with the same identity loses here rather than stealing the link.
        try:
            with transaction.atomic():
                if not SocialAccount.objects.filter(provider=GOOGLE, uid=uid).exists():
                    SocialAccount.objects.create(provider=GOOGLE, uid=uid, user=user,
                                                 extra_data={'email': flows.clean(email), 'email_verified': True},
                                                 last_login=timezone.now())
        except IntegrityError:
            logger.warning('google identity %s was linked elsewhere during signup', uid)
    record(request, 'signup_email_verified_google', user, email=flows.clean(email), flow=flows.SIGNUP)
    flows.clear_signup(request)
