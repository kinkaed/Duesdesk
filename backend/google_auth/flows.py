"""Server-side state for the two things Google is used for.

Google proves an address for a signup, or it opens an account that already
exists. Those are different jobs and must never be confused for one another,
so a browser can only be in one of them at a time and the flow is read from the
session and nowhere else. No query parameter, form field or header can select a
flow, which is what makes it impossible for the login callback to complete a
signup or for the verification callback to sign anybody in.
"""
import secrets
from django.utils import timezone

LOGIN = 'login'
SIGNUP = 'signup'

NONCE = 'google_flow_nonce'
FLOW = 'google_flow'                     # which entry point this session started
LOGIN_FLAG = 'google_login'              # set by the login entry point
# The address Google has just proven, held for the length of the session and
# used by the account adapter to decide an address may count as verified.
LOGGED_IN_EMAIL = 'google_verified_email'
PENDING_TOKEN = 'pending_signup'         # the handle for the PendingSignup being verified
# The raw invitation token behind a pending signup. Only its hash is stored with
# the record, so this is what lets "wrong email, go back" return to the
# invitation the secretary arrived with.
INVITE = 'pending_signup_invite'
# The 30-minute logo preview token behind a pending signup. The preview token is
# signed for this session, so it is held here to put the logo back on the form
# if the secretary goes back from the verification page.
LOGO = 'pending_signup_logo'
SIGNUP_ERROR = 'verify_error'            # a message shown on the verification page
CODE_ERROR = 'verify_code_error'
# What the secretary typed, kept so "wrong email, go back" does not lose it.
RETURN = 'signup_return'

# The single Google callback. Both flows land here and are told apart by FLOW.
CALLBACK = '/accounts/google/login/callback/'


def clean(value):
    """A trimmed, lowercased address, or '' for anything unusable."""
    return (value or '').strip().lower() if isinstance(value, str) else ''


def normalize(value):
    return clean(value)


def same(left, right):
    return bool(clean(left)) and clean(left) == clean(right)


def begin_login(request):
    """Enter the sign-in flow, discarding anything left over from a signup."""
    for key in (PENDING_TOKEN, INVITE, LOGO, LOGIN_FLAG, SIGNUP_ERROR, CODE_ERROR):
        request.session.pop(key, None)
    request.session[NONCE] = secrets.token_urlsafe(32)
    request.session[FLOW] = LOGIN
    request.session[LOGIN_FLAG] = timezone.now().isoformat()


def begin_signup(request, token):
    """Enter the verification flow for one pending signup."""
    for key in (PENDING_TOKEN, LOGIN_FLAG, SIGNUP_ERROR, CODE_ERROR):
        request.session.pop(key, None)
    request.session[NONCE] = secrets.token_urlsafe(32)
    request.session[FLOW] = SIGNUP
    request.session[PENDING_TOKEN] = token


def flow_of(request):
    return request.session.get(FLOW)


def finish(request, flow, clear_invite=True):
    """Leave a flow, clearing its state whether it succeeded or not.

    ``clear_invite`` is off for "wrong email, go back", where the invitation and
    the logo are still what this browser is signing up against and must survive
    the trip through the verification page.
    """
    if request.session.get(FLOW) == flow:
        request.session.pop(FLOW, None)
        request.session.pop(NONCE, None)
    if flow == SIGNUP:
        request.session.pop(PENDING_TOKEN, None)
        if clear_invite:
            request.session.pop(INVITE, None)
            request.session.pop(LOGO, None)
    else:
        request.session.pop(LOGIN_FLAG, None)
