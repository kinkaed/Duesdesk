"""Authentication and verification audit; never attribute an unknown person to a tenant."""
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


def details_for(request, method='', reason=''):
    details = {'provider': 'email' if method == 'code' else 'google', 'ip': client_ip(request),
               'timestamp': timezone.now().isoformat()}
    if method:
        details['method'] = method
    if reason:
        details['reason'] = reason
    return details


def record_user(request, action, user, method='', **extra):
    """Write an event for somebody who is a member of an organization.

    Goes to ``ledger.AuditEvent`` like every other ledger event: same table,
    same columns, same shape. Nothing about that table changes to accommodate
    Google.
    """
    details = {**details_for(request, method), **extra}
    access = UserAccess.objects.filter(user=user).first()
    if not access:
        # Without a membership there is no organization to file this under, and
        # inventing one would be a lie. Fall back to the tenantless table.
        return record(request, 'google_no_tenant', user, reason='no membership', method=method)
    try:
        AuditEvent.objects.create(organization_id=access.organization_id,
            actor=user, action=action, entity='User', entity_id=str(user.pk),
            details=json.dumps(details))
    except Exception:
        # An audit failure must never undo a sign-up that has already committed,
        # nor turn a refusal into a crash page.
        logger.exception('Could not persist %s', action)


def record(request, action, user=None, reason='', email='', flow='', method=''):
    """Write a refusal, for which no organization exists and none may be made.

    ``flow`` records which entry point the attempt came from, so a refused
    verification is never confused with a refused sign-in. ``method`` records
    how a signup address was being proven, 'google' or 'code'.

    The address is recorded because repeated attempts against one address are
    worth correlating, but it may belong to a person with no account here.
    """
    details = details_for(request, method, reason)
    try:
        GoogleAuthRejection.objects.create(
            provider='email' if method == 'code' else 'google',
            action=action, reason=(reason or '')[:60], flow=flow, method=method,
            email=(email or '')[:254], user=user, ip=details['ip'])
        logger.warning('%s %s', action, json.dumps(
            {**details, 'flow': flow, 'email': email, 'user': user.pk if user else None,
             'organization': None}))
    except Exception:
        # A refused sign-in must still be a refused sign-in if the log is down.
        logger.exception('Could not persist %s', action)
