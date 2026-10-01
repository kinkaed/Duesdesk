"""The entry points: sign in with Google, verify a signup, and the one callback.

There is a single Google callback for both jobs.
``/accounts/google/login/callback/`` is the only redirect URI configured with
Google. Which job it is doing comes from the session, set by the entry point
that started the round trip and from nothing in the request itself.
"""
import logging

from django.contrib.auth import views as auth
from django.shortcuts import redirect, render
from django.db import IntegrityError
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST, require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie

from . import flows, signup
from .audit import record
from .models import CODE

logger = logging.getLogger('ledger.google_auth')

VERIFY_URL = '/signup/verify/'
LOGIN_URL = '/login/'
NO_ACCOUNT = 'No account found for this Google email. Please sign up first.'
OAUTH_FAILED = 'Google sign-in did not work. Please try again.'
# Recorded when the OAuth client is not configured at all, so a deployment that
# has not set the credentials yet fails closed with a diagnosable reason.
NOT_CONFIGURED = 'provider not configured'
NOT_SET_UP = 'Google sign-in is not available on this deployment yet.'
# Reasons that mean the address Google proved is not the one being registered.
MISMATCH = ('email mismatch', 'identity email mismatch', 'unverified email',
            'no email claim', 'missing identity claim')


class LoginView(auth.LoginView):
    """The ordinary login page, plus one line about a refused Google round trip.

    The form, the fields and the layout are untouched; this only hands the
    refusal, stored in the session by ``login_error``, to the template.
    """

    template_name = 'registration/login.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['google_login_error'] = self.request.session.pop('google_login_error', None)
        return context


def google_login(request):
    """Start a sign-in round trip.

    Distinct from the verification entry point even though both come back to
    the same callback: this one may only ever open an account that exists.
    """
    if request.user.is_authenticated:
        return redirect('/')
    flows.begin_login(request)
    started = _to_google(request)
    return started or login_error(request, NOT_CONFIGURED)


@ensure_csrf_cookie
def google_login_callback(request):
    """The single Google callback for both flows.

    It performs no authentication itself. It checks only that a flow was
    started, then hands the response to allauth, whose adapter decides what the
    address is allowed to do based on that same session flow.
    """
    if flows.flow_of(request) not in (flows.LOGIN, flows.SIGNUP):
        # Without an entry point, nothing about this response may be trusted.
        record(request, 'google.callback_without_flow', reason='no flow in session')
        return redirect(LOGIN_URL)

    from .provider import GoogleOAuth2Adapter
    # This is allauth's own callback view, driven by our provider adapter, which
    # checks the claims before allauth writes anything. The socialaccount adapter
    # it uses reads the flow recorded above.
    try:
        return BoundGoogleCallbackView.adapter_view(GoogleOAuth2Adapter)(request)
    except Exception:
        # A provider reply we cannot even read is still just a refused round
        # trip. The traceback is logged so the cause is not lost, and the
        # secretary gets the page they came from instead of a server error.
        logger.exception('Google round trip failed')
        return login_error(request, 'provider error') if flows.flow_of(request) == flows.LOGIN \
            else verify_error(request, 'provider error')


def _to_google(request):
    """Hand the browser to Google in one hop, from either entry point.

    This is what allauth's own login view does. Doing it here means the flow is
    recorded and the round trip starts in the same request, with no second URL
    for a caller to be sent through.

    Returns None when the OAuth client is not configured, so a deployment with no
    credentials refuses here with a recorded reason instead of sending the
    browser to Google with an empty client id.
    """
    from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter

    provider = GoogleOAuth2Adapter(request).get_provider()
    if not provider.app.client_id or not provider.app.secret:
        return None
    return provider.redirect(request, process='login', data={
        'flow': flows.flow_of(request),
        'nonce': request.session.get(flows.NONCE),
        'pending': request.session.get(flows.PENDING_TOKEN),
    })


@require_http_methods(['GET', 'POST'])
def verify(request):
    """The verification page: prove the address, or start again.

    Also where a refused round trip lands, which is why the reason is read out
    of the session and shown once.
    """
    if request.user.is_authenticated:
        return redirect('/')

    pending = signup.current(request)
    if pending is None:
        flows.finish(request, flows.SIGNUP)
        return render(request, 'registration/verify_email.html',
                      {'pending': None, 'expired': True}, status=410)

    problem = None
    if request.method == 'POST':
        problem, done = _act(request, pending)
        if done:
            return redirect(done)
        # A refused code clears the pending code state, so reload it.
        pending = signup.current(request) or pending

    return render(request, 'registration/verify_email.html', {
        'pending': pending,
        'expired': False,
        'email': pending.email,
        'return_values': _back_values(request, pending),
        'code_live': pending.code_live(),
        'can_resend': pending.can_send_code(),
        'resend_wait': pending.resend_wait(),
        'error': request.session.pop(flows.SIGNUP_ERROR, None),
        'code_error': request.session.pop(flows.CODE_ERROR, None),
        'sent': request.session.pop('verify_code_sent', False),
        'code_error_shown': problem,
    })


def _back_values(request, pending):
    """What "Wrong email? Go back" hands back to the signup form.

    The pending signup already holds everything that was typed apart from the
    password, so the verification page can fill in the back form without the
    secretary retyping anything, and without the values having to survive in a
    hidden field.
    """
    kept = request.session.get(flows.RETURN) or {}
    return {
        'username': kept.get('username') or pending.username,
        'email': kept.get('email') or pending.email,
        'organization_name': kept.get('organization_name') or pending.organization_name,
        'invite': kept.get('invite') or request.session.get(flows.INVITE, ''),
        'logo_token': request.session.get(flows.LOGO, ''),
        'palette': kept.get('palette') or pending.palette or {},
    }


def _act(request, pending):
    """Handle one press on the verification page.

    Returns ``(problem, done)``: a message to show, and where to go next if the
    press finished something rather than raising a problem.
    """
    action = request.POST.get('action')
    if action == 'resend':
        problem = signup.send_code(request, pending)
        if problem:
            record(request, 'signup.code_rejected', reason=next(
                (key for key, value in signup.MESSAGES.items() if value == problem),
                'send rejected'), email=pending.email, flow=flows.SIGNUP, method=CODE)
            request.session[flows.SIGNUP_ERROR] = problem
        else:
            request.session['verify_code_sent'] = True
        return problem, None
    if action == 'code':
        problem = signup.check_code(request, pending, request.POST.get('code'))
        if problem:
            signup.refuse(request, 'code', pending, reason=next(
                (key for key, value in signup.MESSAGES.items() if value == problem),
                'code rejected'), method=CODE)
            request.session.pop(flows.SIGNUP_ERROR, None)
            request.session[flows.CODE_ERROR] = problem
            return problem, None
        try:
            signup.complete(request, pending, CODE)
        except (signup.Invalid, signup.AlreadyVerified, IntegrityError, ValidationError) as error:
            problem = str(error) if isinstance(error, signup.Invalid) else (
                'We could not finish your sign-up. Please request a new code and try again.')
            signup.refuse(request, 'completion', pending, reason='completion failed', method=CODE)
            request.session[flows.SIGNUP_ERROR] = problem
            return problem, None
        return '', '/'
    if action == 'back':
        # Everything that was typed is already on the pending signup, so it is
        # read from there rather than trusted back from the browser. The password
        # was never stored and is deliberately not kept anywhere.
        request.session[flows.RETURN] = {
            'username': pending.username,
            'email': pending.email,
            'organization_name': pending.organization_name,
            'palette': pending.palette or {},
        }
        flows.finish(request, flows.SIGNUP, clear_invite=False)
        return '', '/signup/'
    return '', None


@require_POST
def google_verify(request):
    """Start the Google round trip for the pending signup held in this session.

    Sets the flow flag so the shared callback verifies rather than signs in.
    Creates nothing.
    """
    pending = signup.current(request)
    if pending is None:
        request.session[flows.SIGNUP_ERROR] = signup.MESSAGES['no_pending']
        return redirect(VERIFY_URL)
    if request.user.is_authenticated:
        return redirect('/')
    flows.begin_signup(request, pending.token)
    return _to_google(request) or verify_error(request, NOT_CONFIGURED)


def login_error(request, reason, user=None, email=''):
    """Refuse a sign-in and say so on the login page.

    An address with no account behind it gets a plain invitation to sign up.
    Nothing is created and no pending signup is started.
    """
    record(request, 'google.login_rejected', user, reason=reason, email=email,
           flow=flows.LOGIN, method='google')
    flows.finish(request, flows.LOGIN)
    if reason in ('no matching account', 'no email claim', 'unverified email'):
        # Nothing here says anything about the address; it is simply unknown here.
        request.session['google_login_error'] = NO_ACCOUNT
    elif reason == NOT_CONFIGURED:
        # Not the secretary's fault, and not about their address at all.
        request.session['google_login_error'] = NOT_SET_UP
    else:
        request.session['google_login_error'] = OAUTH_FAILED
    return redirect(LOGIN_URL)


def verify_error(request, reason, email=''):
    """Refuse a verification and say so on the verification page.

    An address Google proved that is not the address being registered is
    refused here, and the page offers the emailed code instead.
    """
    signup.refuse(request, 'google', reason=reason, email=email, method='google')
    # A reason that is one of our own messages is shown as it stands; anything
    # else is a mismatch, an unreadable reply, or a deployment with no OAuth
    # client, which is not about the address at all.
    if reason == NOT_CONFIGURED:
        message = NOT_SET_UP
    elif reason in signup.MESSAGES.values():
        message = reason
    elif reason in MISMATCH:
        message = signup.GOOGLE_MISMATCH
    else:
        message = signup.MESSAGES['no_pending']
    request.session[flows.SIGNUP_ERROR] = message
    return redirect(VERIFY_URL)


from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView


class BoundGoogleCallbackView(OAuth2CallbackView):
    """Check the server-stored OAuth state before exchanging any credentials."""

    def _get_state(self, request, provider):
        state, response = super()._get_state(request, provider)
        if response:
            return state, response
        data = state.get('data') or {}
        expected = {'flow': flows.flow_of(request),
                    'nonce': request.session.get(flows.NONCE),
                    'pending': request.session.get(flows.PENDING_TOKEN)}
        if not expected['nonce'] or data != expected:
            record(request, 'google.callback_rejected', reason='stale or crossed flow',
                   flow=data.get('flow', ''), method='google')
            if flows.flow_of(request) == flows.SIGNUP:
                request.session[flows.SIGNUP_ERROR] = 'That Google request has expired. Please try again.'
                return None, redirect(VERIFY_URL)
            request.session['google_login_error'] = 'That Google request has expired. Please try again.'
            return None, redirect(LOGIN_URL)
        return state, None
