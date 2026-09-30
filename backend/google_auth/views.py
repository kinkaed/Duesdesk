"""Expose only the Google redirect and callback; reuse the existing error UI."""
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.http import QueryDict
from django.shortcuts import render
from django.views.decorators.http import require_GET
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView
from .audit import record
from .provider import GoogleOAuth2Adapter

oauth2_login = OAuth2LoginView.adapter_view(GoogleOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(GoogleOAuth2Adapter)


def login_error(request):
    form = AuthenticationForm(request, data={})
    form.is_valid()
    form.add_error(None, 'Unable to sign in. Please try again.')
    return render(request, 'registration/login.html', {'form': form, 'next': '/'}, status=403)


def configured():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


@require_GET
def google_login(request):
    if not configured():
        record(request, 'google_login_rejected', reason='provider not configured')
        return login_error(request)
    # Do not accept client-controlled scope, access_type, process or redirects.
    request.GET = QueryDict('')
    return oauth2_login(request)


@require_GET
def google_callback(request):
    if not configured():
        record(request, 'google_login_rejected', reason='provider not configured')
        return login_error(request)
    return oauth2_callback(request)
