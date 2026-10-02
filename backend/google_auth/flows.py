"""Server-side state for the one thing signup needs: which pending signup this
browser is verifying, and what it typed to get here.

Signup creates an account only after an emailed code proves the address, so a
browser is either verifying a pending signup or doing something else. The link
between the two is the unguessable token stored here and nowhere else: no query
parameter, form field or header can name a pending signup, which is what makes it
impossible to complete somebody else's signup.
"""
import secrets

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


def clean(value):
    """A trimmed, lowercased address, or '' for anything unusable."""
    return (value or '').strip().lower() if isinstance(value, str) else ''


def normalize(value):
    return clean(value)


def same(left, right):
    return bool(clean(left)) and clean(left) == clean(right)


def begin_signup(request, token):
    """Enter the verification flow for one pending signup."""
    for key in (PENDING_TOKEN, SIGNUP_ERROR, CODE_ERROR):
        request.session.pop(key, None)
    request.session[PENDING_TOKEN] = token


def finish(request, clear_invite=True):
    """Leave the verification flow, clearing its state whether it succeeded or not.

    ``clear_invite`` is off for "wrong email, go back", where the invitation and
    the logo are still what this browser is signing up against and must survive
    the trip through the verification page.
    """
    request.session.pop(PENDING_TOKEN, None)
    if clear_invite:
        request.session.pop(INVITE, None)
        request.session.pop(LOGO, None)


def new_nonce():
    """A fresh unguessable value, for callers that need one."""
    return secrets.token_urlsafe(32)