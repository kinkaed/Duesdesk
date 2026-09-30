"""Authentication audit; never attribute an unknown person to a tenant."""
import ipaddress
import json
import logging

from django.utils import timezone

from ledger.models import AuditEvent, UserAccess

from .models import GoogleAuthRejection

logger = logging.getLogger('ledger.google_auth')


def client_ip(request):
    """Only the address the application server observed.

    Forwarded headers are deliberately ignored: they are attacker-supplied
    unless a trusted proxy in front of the application rewrites them, and an
    audit record must not depend on that trust decision.
    """
    raw = (request.META.get('REMOTE_ADDR') or '').strip()
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ''


def details_for(request, reason):
    details = {'provider': 'google', 'ip': client_ip(request),
               'timestamp': timezone.now().isoformat()}
    if reason:
        details['reason'] = reason
    return details


def record(request, action, user=None, reason=None, email=''):
    """Write one authentication event to the trail it can honestly belong to.

    A rejection by a known member belongs to that member's organization and is
    stored in ``ledger.AuditEvent`` like every other ledger event. Anything
    else -- an unknown address, an unverified claim, an unconfigured provider,
    an account with no membership -- belongs to no organization and is stored
    in ``GoogleAuthRejection``. No organization is ever invented or borrowed.

    ``email`` is the address Google presented, which may belong to a person
    who has no account here; it is recorded so that repeated attempts against
    one address can be correlated.
    """
    details = details_for(request, reason)
    access = UserAccess.objects.filter(user=user).first() if user else None
    try:
        if access:
            AuditEvent.objects.create(organization_id=access.organization_id,
                actor=user, action=action, entity='User',
                entity_id=str(user.pk), details=json.dumps(details))
        else:
            GoogleAuthRejection.objects.create(
                action=action, reason=reason or '', email=(email or '')[:254],
                user=user, ip=details['ip'])
            logger.warning('%s %s', action, json.dumps(
                {**details, 'email': email, 'user': user.pk if user else None,
                 'organization': None}))
    except Exception:
        # An audit failure must never turn a refused sign-in into a crash page,
        # nor undo a sign-in that has already been completed.
        logger.exception('Could not persist %s', action)
