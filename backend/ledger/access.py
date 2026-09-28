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
    if not access:
        return Member.objects.none()
    rows = Member.objects.filter(organization_id=access.organization_id)
    if access.role in ('secretary', 'auditor'):
        return rows
    return rows.filter(pk=access.member_id) if access.role == 'member' else rows.none()


def visible_payments(user):
    org = organization_for(user)
    return Payment.objects.filter(organization=org, member__in=visible_members(user)) if org else Payment.objects.none()
