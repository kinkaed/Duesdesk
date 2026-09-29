from .models import Member, Payment, UserAccess


def membership(user):
    if not user.is_authenticated or not user.is_active:
        return None
    return UserAccess.objects.select_related('organization').filter(user=user, active=True).first()


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
