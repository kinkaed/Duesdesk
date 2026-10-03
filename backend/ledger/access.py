from .models import Member, Payment, UserAccess

# The key Django's auth framework writes into a session to remember who is signed
# in. Reading it is the only supported way to map a stored session back to an
# account, because the session table is keyed by an opaque session key and has no
# user column of its own.
AUTH_USER_KEY = '_auth_user_id'


def revoke_sessions(user):
    """Delete every stored session belonging to `user`, and return how many.

    Clearing ``is_active`` stops a disabled account passing authenticate() and
    stops membership() from resolving, but it does not touch the rows in
    django_session. A session established before the account was disabled
    therefore survives the change, and re-enabling the account would silently
    make that same cookie work again. Deleting the rows is what makes "disabled
    means signed out everywhere" true rather than merely probable.

    A signed-in user normally has one session per browser, so a phone, a laptop
    and a second tab are three separate rows and all three have to go.

    Django offers no lookup by user, so the sessions are scanned and matched on
    the decoded payload. An expired row cannot authenticate anything, but it is
    still this account's data, so it is included. A row that cannot be decoded is
    skipped rather than treated as a failure: one corrupt row must not be able to
    block an account being disabled, and an undecodable row cannot belong to this
    user in any way that matters.

    Failures other than decoding are deliberately not swallowed. Revoking the
    sessions is the security control; quietly continuing without it would report
    "disabled" while leaving the old cookies working, which is the exact failure
    this function exists to prevent.
    """
    from django.contrib.sessions.models import Session

    user_pk = str(getattr(user, 'pk', '') or '')
    if not user_pk:
        return 0
    doomed = []
    for session in Session.objects.only('pk', 'session_data').iterator():
        try:
            decoded = session.get_decoded()
        except Exception:
            continue
        if decoded.get(AUTH_USER_KEY) == user_pk:
            doomed.append(session.pk)
    if not doomed:
        return 0
    Session.objects.filter(pk__in=doomed).delete()
    return len(doomed)


def membership(user):
    if not user.is_authenticated or not user.is_active:
        return None
    # A disabled organization resolves to no membership, which is what makes every
    # tenant query and endpoint refuse it at once: organization_for, role_for,
    # visible_members and visible_payments all go through here. The audit rows and
    # the membership row itself are left in place, so the history survives.
    return UserAccess.objects.select_related('organization').filter(
        user=user, active=True, organization__disabled_at__isnull=True).first()


def organization_for(user):
    access = membership(user)
    return access.organization if access else None


def last_access_organization(user):
    """The organization this account is most closely associated with, active or not.

    membership() deliberately returns nothing once access has lapsed, which is
    what protects every tenant query. But "an account that lost its access and
    then hit a data endpoint anyway" is precisely the event an auditor needs to
    see, and dropping it to the technical log is how it stayed invisible: there
    was no organization to file it under, so audit() refused to write it.

    Preferring an active row matters for the other reason access ends — the Django
    is_active flag being cleared while the access row is still active — where the
    organization is still perfectly well defined.
    """
    if not user.is_authenticated:
        return None
    access = (UserAccess.objects.select_related('organization')
              .filter(user=user).order_by('-active', '-pk').first())
    return access.organization if access else None


def role_for(user):
    access = membership(user)
    return access.role if access else None


def visible_members(user):
    access = membership(user)
    # Only an active secretary or auditor sees member records. Members are data
    # rows, not accounts, so there is no role that narrows this to one member.
    if not access or access.role not in ('secretary', 'auditor'):
        return Member.objects.none()
    return Member.objects.filter(organization_id=access.organization_id)


def visible_payments(user):
    org = organization_for(user)
    return Payment.objects.filter(organization=org, member__in=visible_members(user)) if org else Payment.objects.none()
