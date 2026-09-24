from .models import Member, Payment

def role_for(user):
    if not user.is_authenticated or not user.is_active:
        return None
    try:
        return user.access.role
    except AttributeError:
        return 'secretary' if user.is_staff or user.is_superuser else None

def visible_members(user):
    role = role_for(user)
    if role in ('secretary', 'auditor'):
        return Member.objects.all()
    if role == 'member':
        return Member.objects.filter(pk=user.access.member_id)
    return Member.objects.none()

def visible_payments(user):
    return Payment.objects.filter(member__in=visible_members(user))
