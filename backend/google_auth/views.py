"""The email-code verification page: prove an address, or start again.

This is the only unauthenticated view that creates an account, and it creates one
only after a code emailed to the pending signup's own address has been entered
correctly. Everything it does is delegated to :mod:`google_auth.signup`; this
module is the request/response shell around it.
"""
import logging

from django.shortcuts import redirect, render
from django.db import IntegrityError
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_http_methods

from . import flows, signup
from .audit import record
from .models import CODE

logger = logging.getLogger('ledger.auth')

VERIFY_URL = '/signup/verify/'


@require_http_methods(['GET', 'POST'])
def verify(request):
    """The verification page: enter the code, resend it, or go back to signup."""
    if request.user.is_authenticated:
        return redirect('/')

    pending = signup.current(request)
    if pending is None:
        flows.finish(request)
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
                'send rejected'), email=pending.email, flow='signup', method=CODE)
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
            signup.complete(request, pending)
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
        flows.finish(request, clear_invite=False)
        return '', '/signup/'
    return '', None