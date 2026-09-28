from django.conf import settings
from django.core.exceptions import ValidationError
from .models import Organisation
from .access import role_for, organization_for
from .branding import branding_json


def site_context(request):
    org=organization_for(request.user)
    if not org and request.path=='/login/' and request.GET.get('org'):
        try:org=Organisation.objects.filter(public_id=request.GET['org']).first()
        except (ValueError,ValidationError):pass
    return {'demo_mode':settings.DEMO_MODE,'role':role_for(request.user),'organisation':org,
            'branding':branding_json(org) if org else None}
