"""Backend-only extensions: existing form fields and templates stay unchanged."""
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.contrib.auth import get_user_model
from axes.handlers.proxy import AxesProxyHandler

from .models import LegacyIdentity


class GuardedAuthenticationForm(AuthenticationForm):
    def clean(self):
        credentials = {'username': self.cleaned_data.get('username', '')}
        if not AxesProxyHandler.is_allowed(self.request, credentials):
            self.request.axes_locked_out = True
            self.request.axes_credentials = credentials
            raise self.get_invalid_login_error()
        return super().clean()


class AccountRecoveryForm(PasswordResetForm):
    """Password reset, extended for accounts that arrived with no password.

    Django deliberately refuses to send a reset link to an account with an
    unusable password: if this server never proved that address, an emailed link
    must not be able to claim it. That rule is kept.

    The exception is an account the external provider created and the removal
    migration recorded in ``LegacyIdentity``. The address was proven while that
    provider existed, and without this those people could never sign in at all.
    The grant is spent once: the row is deleted when the password is set.
    """

    def get_users(self, email):
        seen = set()
        for user in super().get_users(email):
            seen.add(user.pk)
            yield user
        candidates = (get_user_model().objects
                      .filter(email__iexact=email, is_active=True)
                      .exclude(pk__in=seen))
        for user in candidates:
            if user.has_usable_password():
                continue
            if LegacyIdentity.objects.filter(user=user).exists():
                yield user