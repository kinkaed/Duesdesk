"""Auth views that record what happened, without recording credentials.

Django's auth views are the one place where a request is interesting before any
middleware downstream has decided who the user is, so they are subclassed rather
than watched with signals. A signal sees a User or a request and no knowledge of
whether the attempt actually succeeded; a subclass knows exactly which branch of
form_valid ran.

Two rules hold across every view here:

* A failed login is recorded against a *hashed* identifier, never the username.
  The audit history is readable by auditors, so writing the attempted username
  would turn the history into a directory of who is being targeted, and would
  confirm to a reader whether a given account exists. The hash is stable enough
  to correlate repeated attempts against the same account and useless for
  reversing.
* The password is never read. The submitted credentials are not passed to audit()
  or to the technical log; form.cleaned_data is deliberately not used.
"""
import hashlib
import logging

from django.contrib.auth import views as auth

from . import views
from .access import organization_for
from .observability import technical
from .services import audit

logger = logging.getLogger('ledger.auth')

# Short, unsalted on purpose: this is a correlation handle inside an already
# access-controlled table, not a credential store, and a salt per row would make
# "show me every failure against this account" impossible.
def username_fingerprint(username):
    username = (username or '').strip()
    if not username:
        return ''
    return hashlib.sha256(username.lower().encode('utf-8')).hexdigest()[:16]


def _locked_out(username, ip_address):
    """True when axes would refuse the next attempt for this username.

    Read through axes rather than assumed, so the audit trail distinguishes "wrong
    password" from "this account is now locked", which are very different events
    for whoever is reviewing it. Any failure to answer simply means unknown.
    """
    try:
        from axes.utils import get_failure_limit
        from axes.handlers.database import get_user_attempts
        return get_user_attempts(username=username, ip_address=ip_address) >= get_failure_limit()
    except Exception:
        return False


class AuditableLoginView(auth.LoginView):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Surfaces a Google round-trip refusal stored in the session by
        # google_auth.login_error; the form, fields and layout are untouched.
        context['google_login_error'] = self.request.session.pop('google_login_error', None)
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.get_user()
        # organization= is passed explicitly so the event names the tenant the
        # login actually granted access to, which is not the same as "a user
        # exists". The IP and user agent are already columns on the row.
        audit(user, 'auth.login.success', outcome='success',
              organization=organization_for(user) if user and user.is_authenticated else None,
              resource='User', resource_id=str(getattr(user, 'pk', '') or ''))
        return response

    def form_invalid(self, form):
        username = (self.request.POST.get('username') or '').strip()
        ip_address = self.request.META.get('REMOTE_ADDR') or None
        locked = _locked_out(username, ip_address)
        # organization=None is not a shortcut here: before authentication there
        # is no organization to name, and these actions are on the anonymous
        # allowlist precisely so the row is written without inventing a tenant.
        audit(None, 'security.account_locked' if locked else 'auth.login.failure',
              outcome='denied' if locked else 'failure',
              reason='account_locked' if locked else 'invalid_credentials',
              details={'username_fingerprint': username_fingerprint(username)})
        technical('auth.login.failed', 'Login attempt failed',
                  username_fingerprint=username_fingerprint(username),
                  reason='account_locked' if locked else 'invalid_credentials',
                  locked=locked)
        return super().form_invalid(form)


class AuditableLogoutView(auth.LogoutView):
    def dispatch(self, request, *args, **kwargs):
        # The user is still authenticated here: LogoutView only clears the session
        # once dispatch has started, so the event can name both who left and where.
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            audit(user, 'auth.logout', outcome='success')
        return super().dispatch(request, *args, **kwargs)


class AuditablePasswordChangeView(auth.PasswordChangeView):
    def form_valid(self, form):
        # PasswordChangeForm has no get_user(): it is bound to request.user by the
        # view, and that is still the account whose password just changed.
        user = self.request.user
        response = super().form_valid(form)
        audit(user, 'auth.password.changed', outcome='success')
        return response

    def form_invalid(self, form):
        user = getattr(self.request, 'user', None)
        if user is not None and user.is_authenticated:
            # The form's errors are never copied: they echo the submitted
            # password fields back, and an audit row is read by auditors.
            audit(user, 'auth.password.change_failed', outcome='rejected',
                  reason='validation_failed')
        return super().form_invalid(form)


class AuditablePasswordResetConfirmView(auth.PasswordResetConfirmView):
    def form_valid(self, form):
        response = super().form_valid(form)
        # Django signs the user in on this branch, so the account is known.
        user = form.get_user()
        audit(user, 'auth.password_reset.completed', outcome='success',
              organization=organization_for(user) if user and user.is_authenticated else None)
        return response

    def form_invalid(self, form):
        technical('auth.password_reset.rejected', 'Password reset rejected',
                  reason='invalid_or_expired_link')
        return super().form_invalid(form)


class AuditableRecoveryView(views.RecoveryView):
    """The throttled recovery view, plus the audit event.

    Subclasses views.RecoveryView rather than PasswordResetView so the one-request
    per five minutes limit and the "never reveal whether the address exists"
    behaviour are preserved exactly. The audit event is written after the base
    view has already decided to redirect, so recording it cannot change what the
    caller is told.
    """

    def form_valid(self, form):
        response = super().form_valid(form)
        # Requested before anyone is authenticated, so there is no user and no
        # organization. The fingerprint lets an auditor see that repeated
        # requests are coming for one account without disclosing the address
        # being targeted, and the response is identical either way, so this
        # cannot be used to discover which accounts exist.
        email = (self.request.POST.get('email') or '').strip()
        audit(None, 'auth.recovery.requested', outcome='success',
              details={'email_fingerprint': username_fingerprint(email)})
        return response
