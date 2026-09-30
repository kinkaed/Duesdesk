"""Server-side state for the two Google flows.

Google does two unrelated things here, so it needs two entry points, two
callbacks and a strict record of which one a browser is in the middle of.
The flow is read from the session only. Nothing in here is ever taken from a
query parameter, a form field or the client, so the signup callback can never
log anybody in and the login callback can never start a signup.
"""
from datetime import timedelta

from django.utils import timezone

LOGIN = 'login'
SIGNUP = 'signup'

FLOW = 'google_flow'                    # which entry point this session started
PENDING = 'signup_email_verification'   # the address typed into the signup form
VERIFIED = 'verified_signup_email'      # {'email': ..., 'at': iso}
LOGIN_FLAG = 'google_login'             # set by the login entry point
LOGGED_IN_EMAIL = 'google_verified_email'
SIGNUP_UID = 'signup_google_uid'        # Google's identity, to link at signup
SIGNUP_ERROR = 'google_signup_error'    # message shown on the signup page
INVITE = 'signup_invite'

TTL = timedelta(minutes=15)

# Verdicts returned by verification().
OK = 'ok'
MISSING = 'missing'
EXPIRED = 'expired'
MISMATCH = 'mismatch'


def clean(email):
    """An address as it should be stored and shown: trimmed, casing intact."""
    return str(email or '').strip()


def normalize(email):
    """The comparison key for an address, which mail treats case-insensitively."""
    return clean(email).casefold()


def same(left, right):
    """Compare two addresses case-insensitively, but without trimming.

    Whitespace stays significant on purpose. An address carrying a stray space
    is not the address that was proven, and treating it as the same one would
    widen a verification grant that was made for an exact string.
    """
    return bool(left) and str(left).casefold() == str(right or '').casefold()


def begin(request, flow):
    """Start a flow, discarding any proof left over from an earlier one."""
    for key in (PENDING, VERIFIED, LOGIN_FLAG, SIGNUP_UID):
        request.session.pop(key, None)
    request.session[FLOW] = flow
    if flow == LOGIN:
        request.session[LOGIN_FLAG] = timezone.now().isoformat()


def flow_of(request):
    return request.session.get(FLOW)


def is_flow(request, flow):
    return request.session.get(FLOW) == flow


def finish(request, flow):
    if request.session.get(FLOW) == flow:
        request.session.pop(FLOW, None)


def expired(at):
    try:
        moment = timezone.datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return True
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return moment < timezone.now() - TTL


def grant(request, email):
    request.session[VERIFIED] = {'email': normalize(email), 'at': timezone.now().isoformat()}


def verification(request, email):
    """Classify the stored proof against the address being submitted.

    Returns ``OK`` only for an unexpired proof of exactly this address, so a
    secretary who edits the email field after verifying must verify again.
    """
    entry = request.session.get(VERIFIED)
    if not isinstance(entry, dict) or not entry.get('email'):
        return MISSING
    if expired(entry.get('at')):
        request.session.pop(VERIFIED, None)
        request.session.pop(SIGNUP_UID, None)
        return EXPIRED
    if normalize(entry['email']) != normalize(email):
        return MISMATCH
    return OK


def verified_email(request):
    """The address proven with Google, or None once it is stale.

    A pure read: the signup page renders this on every GET, so it must not
    consume or discard the proof. Only ``verification`` and ``begin`` clear it.
    """
    entry = request.session.get(VERIFIED)
    if not isinstance(entry, dict) or not entry.get('email'):
        return ''
    if expired(entry.get('at')):
        return ''
    return normalize(entry['email'])


def signup_uid(request):
    return request.session.get(SIGNUP_UID) or ''


def clear_signup(request):
    for key in (PENDING, VERIFIED, SIGNUP_UID, FLOW):
        request.session.pop(key, None)
