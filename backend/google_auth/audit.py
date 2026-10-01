"""Authentication and verification audit, on the ledger's own write path.

Nothing here writes to ``ledger.AuditEvent`` directly. Every event goes through
``ledger.services.audit``, the same helper the rest of the application uses, so a
Google event carries the same correlation the technical log does: request id,
method, path, client address and user agent, plus redaction and a size cap on the
details. Writing the row here instead would have produced an event that looked
like every other one in the history and quietly lacked the columns that make an
audit row evidence.

Two kinds of event, and they are kept apart on purpose:

* Something a member did (``record_user``). The account has a membership, so the
  event is filed under that organization and is visible in the audit history.
* Something that was refused (``record``). A refusal has no organization of its
  own, so it is written to ``GoogleAuthRejection``, which is deliberately
  tenantless. A null organization is never invented to make such a row appear in
  a tenant's history: a row no tenant can read is not a control, it is a row
  nobody reviews. When the attempt did resolve to a real account, the refusal is
  *also* filed under that account's organization, because that is a security
  event the organization can and should see.
"""
import logging

from ledger.models import UserAccess
from ledger.observability import client_ip, technical
from ledger.services import audit as ledger_audit

from .models import GoogleAuthRejection

logger = logging.getLogger('ledger.google_auth')

# The resource type both kinds of event concern. Every action here is about an
# account rather than an organization or a member.
RESOURCE = 'User'


def details_for(request, method='', flow='', email='', **extra):
    """The provider-specific part of an event, before redaction.

    The client address and the time are not here: they are columns on the audit
    row, filled from the request context, and repeating them inside the details
    JSON would give two places to disagree.
    """
    details = {'provider': 'email' if method == 'code' else 'google'}
    if method:
        details['method'] = method
    if flow:
        details['flow'] = flow
    if email:
        # Recorded because repeated attempts against one address are worth
        # correlating, and because a mismatch between the claimed address and the
        # verified one is the interesting fact in a refusal. Redaction does not
        # touch it, which is intended: it is an address, not a credential.
        details['email'] = email[:254]
    # Anything the caller adds explicitly, so a future fact can be attached
    # without editing this function. Redaction is applied by services.audit.
    details.update(extra)
    return details


def record_user(request, action, user, method='', reason='', **extra):
    """Record an event about an account that belongs to an organization.

    Returns None, and records the refusal instead, when the account has no
    membership to file the event under.
    """
    access = UserAccess.objects.filter(user=user).first()
    if not access:
        # Without a membership there is no organization to file this under, and
        # inventing one would be a lie. Recorded as a refusal with no tenant.
        return record(request, 'google.no_tenant', user, reason='no_active_membership',
                      method=method)
    # No try/except: services.audit is specified never to raise, and it is the
    # only caller allowed to decide that an audit row could not be written.
    return ledger_audit(user, action, details={**details_for(request, method, **extra)},
                        resource=RESOURCE, resource_id=str(user.pk),
                        reason=reason, organization=access.organization)


def record(request, action, user=None, reason='', email='', flow='', method=''):
    """Record a refusal, for which no organization exists and none may be made.

    ``flow`` records which entry point the attempt came from, so a refused
    verification is never confused with a refused sign-in. ``method`` records how
    a signup address was being proven, 'google' or 'code'.

    The address is recorded because repeated attempts against one address are
    worth correlating, but it may belong to a person with no account here.

    Never raises. A refused sign-in must still be a refused sign-in if the
    database is unavailable, and the exception is kept in the technical log.
    """
    try:
        GoogleAuthRejection.objects.create(
            provider='email' if method == 'code' else 'google',
            action=action, reason=(reason or '')[:60], flow=flow, method=method,
            email=(email or '')[:254], user=user, ip=client_ip(request) or '')
    except Exception:
        logger.exception('Could not persist %s', action)

    if user is not None:
        # The attempt resolved to an account this server owns, so the
        # organization that account belongs to can see it. A member being refused
        # a sign-in is exactly the event that belongs in their security history.
        access = UserAccess.objects.filter(user=user).first()
        if access:
            ledger_audit(user, action,
                         details=details_for(request, method, flow, email),
                         outcome='rejected', reason=reason, organization=access.organization,
                         resource=RESOURCE, resource_id=str(user.pk))

    # A technical line as well, so a refusal is visible to whoever is watching
    # the logs even when it has no organization to be filed under. The audit
    # helper's own log lines cannot carry this: it is called with the event, not
    # with a message about what could not be attributed.
    technical('google_auth.refused', action, level=logging.WARNING,
              outcome='rejected', reason=reason, provider='google', flow=flow,
              method=method, email=email or None, actor_id=getattr(user, 'pk', None),
              organization_id=None)
