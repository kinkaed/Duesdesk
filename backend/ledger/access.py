from .models import Member, Payment, UserAccess


def membership(user):
    if not user.is_authenticated or not user.is_active:
        return None
    return UserAccess.objects.select_related('organization').filter(user=user, active=True).first()


def organization_for(user):
    access = membership(user)
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
