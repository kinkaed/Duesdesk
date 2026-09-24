from django.conf import settings
from .models import Organisation
from .access import role_for

def site_context(request):
    return {'demo_mode': settings.DEMO_MODE, 'role': role_for(request.user), 'organisation': Organisation.objects.first()}
