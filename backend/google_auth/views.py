"""Two entry points, two callbacks, and the existing screens for refusals."""
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.http import QueryDict, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView
from . import flows
from .audit import record
from .provider import GoogleOAuth2Adapter

oauth2_login = OAuth2LoginView.adapter_view(GoogleOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(GoogleOAuth2Adapter)

LOGIN_REFUSAL = {
    'default': 'Unable to sign in. Please try again.',
    'unverified email': 'Google has not verified that email address. Please try again.',
    'no matching account': 'No Duesdesk account uses that email address. Please sign up first.',
    'ambiguous email': 'No Duesdesk account uses that email address. Please sign up first.',
    'inactive account': 'This account is not available. Please contact your secretary.',
    'identity email mismatch': 'That Google account is already linked to a different Duesdesk account. Please sign in with your password.',
    'different Google identity already linked': 'This Duesdesk account already uses a different Google account. Please sign in with your password.',
    'session account mismatch': 'Sign out before signing in with a different Google account.',
    'email already linked': 'That email address is already used by another Duesdesk account. Please sign in with your password.',
    'identity conflict': 'That Google account could not be linked. Please try again.',
    'unknown flow': 'That sign-in attempt is no longer valid. Please try again.',
    'wrong flow': 'That sign-in attempt is no longer valid. Please try again.',
    'missing identity claim': 'Google did not return an email address. Please try again.',
}

SIGNUP_REFUSAL = {
    'default': 'Unable to verify your email address with Google. Please try again.',
    'email mismatch': 'The Google account email does not match the email entered',
    'unverified email': 'Google has not verified that email address. Please try again.',
    'missing identity claim': 'Google did not return an email address. Please try again.',
    'account already exists': 'An account already exists for this email. Please sign in instead.',
    'identity already linked': 'That Google account is already used by another Duesdesk account. Please sign in instead.',
    'invalid login process': 'That attempt is no longer valid. Please try again.',
    'unknown flow': 'That attempt is no longer valid. Please try again.',
    'wrong flow': 'That attempt is no longer valid. Please try again.',
    'no signup email': 'Enter your email address, then verify it with Google.',
    'provider not configured': 'Google sign-in is not set up yet. Please try again later.',
}

EXPIRED_SIGNUP = 'Your Google verification has expired. Please verify this email address with Google again.'
UNVERIFIED_SIGNUP = 'Verify this email address with Google before creating your account.'


def login_error(request, reason='default', user=None, email=''):
    """The existing login screen, carrying a reason in its existing error slot."""
    record(request, 'google_login_rejected', user, reason, email, flow=flows.LOGIN)
    form = AuthenticationForm(request, data={})
    form.is_valid()
    form.add_error(None, 'Unable to sign in. Please try again.')
    return render(request, 'registration/login.html',
                  {'form': form, 'next': '/',
                   'google_error': LOGIN_REFUSAL.get(reason, LOGIN_REFUSAL['default'])},
                  status=403)


def signup_target(request):
    invite = request.session.get(flows.INVITE) or ''
    return '/signup/' + ('?invite=' + invite if invite else '')


def signup_error(request, reason='default', email=''):
    """Refuse to prove an address, and hand the reason back to the signup page."""
    record(request, 'google_signup_rejected', None, reason, email, flow=flows.SIGNUP)
    request.session[flows.SIGNUP_ERROR] = SIGNUP_REFUSAL.get(reason, SIGNUP_REFUSAL['default'])
    return HttpResponseRedirect(signup_target(request))


def configured():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


@require_GET
def google_login(request):
    """Login entry point. Sets the session flag the callback checks."""
    if not configured():
        return login_error(request, 'provider not configured')
    flows.begin(request, flows.LOGIN)
    # Do not accept client-controlled scope, access_type, process or redirects.
    request.GET = QueryDict('')
    return oauth2_login(request)


@require_POST
def google_signup_verify(request):
    """Signup entry point. Records the address the secretary typed.

    The button posts the signup form itself, so the address arrives with the
    other fields. Nothing is created here: this only remembers what will have
    to be proven before the signup may proceed.
    """
    if not configured():
        return signup_error(request, 'provider not configured')
    email = flows.normalize(request.POST.get('email', ''))
    if not email:
        return signup_error(request, 'no signup email')
    flows.begin(request, flows.SIGNUP)
    request.session[flows.PENDING] = email
    request.session[flows.INVITE] = request.POST.get('invite') or request.GET.get('invite') or ''
    request.GET = QueryDict('')
    return oauth2_login(request)


@require_GET
def google_login_callback(request):
    if not configured():
        return login_error(request, 'provider not configured')
    if not flows.is_flow(request, flows.LOGIN):
        return login_error(request, 'wrong flow')
    return oauth2_callback(request)


@require_GET
def google_signup_verify_callback(request):
    if not configured():
        return signup_error(request, 'provider not configured')
    if not flows.is_flow(request, flows.SIGNUP):
        return signup_error(request, 'wrong flow')
    return oauth2_callback(request)
