"""Backend-only extensions: existing form fields and templates stay unchanged."""
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.contrib.auth import get_user_model
from axes.handlers.proxy import AxesProxyHandler
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount


class GuardedAuthenticationForm(AuthenticationForm):
    def clean(self):
        credentials = {'username': self.cleaned_data.get('username', '')}
        if not AxesProxyHandler.is_allowed(self.request, credentials):
            self.request.axes_locked_out = True
            self.request.axes_credentials = credentials
            raise self.get_invalid_login_error()
        return super().clean()


class GooglePasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        # Preserve Django's usual eligibility, adding only verified Google users
        # with unusable passwords. Never verify an unrelated contact address.
        seen = set()
        for user in super().get_users(email):
            seen.add(user.pk)
            yield user
        for user in get_user_model().objects.filter(email__iexact=email, is_active=True):
            if user.pk in seen or user.has_usable_password():
                continue
            if (EmailAddress.objects.filter(user=user, email__iexact=user.email, verified=True).exists()
                    and SocialAccount.objects.filter(user=user, provider='google', extra_data__email__iexact=user.email).exists()):
                yield user
