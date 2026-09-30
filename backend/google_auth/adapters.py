"""Google may authenticate an existing account, but may never create one."""
from django.contrib.auth import get_user_model, login
from django.db import transaction, IntegrityError
from django.http import HttpResponseRedirect
from django.utils import timezone
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount
from ledger.models import UserAccess
from .audit import record

GOOGLE = 'google'
ROLES = ('secretary', 'auditor')
BACKEND = 'django.contrib.auth.backends.ModelBackend'


class AccountAdapter(DefaultAccountAdapter):
    """Only the address just proven to Google is treated as verified."""

    def is_email_verified(self, request, email):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated or not email:
            return False
        user_email = (user.email or '').casefold()
        if not user_email or user_email != email.casefold():
            return False
        if request.session.get('google_verified_email', '') != user_email:
            return False
        return EmailAddress.objects.filter(user=user, email__iexact=user_email,
                                           verified=True).exists()

    def is_open_for_signup(self, request):
        return False


class GoogleAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return False

    def authenticate_by_email(self, sociallogin):
        # Our explicit matching also handles legacy duplicate emails safely.
        return None

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        self.reject(request, 'oauth error')

    def reject(self, request, reason, user=None, email=''):
        from .views import login_error
        record(request, 'google_login_rejected', user, reason, email)
        raise ImmediateHttpResponse(login_error(request))

    def validate_claims(self, request, account):
        """Refuse a claim before allauth touches the database.

        Runs from the provider's ``complete_login``, which precedes
        ``SocialLogin.lookup()``. Without this, allauth would overwrite the
        stored ``extra_data`` of an already-linked Google identity before we
        ever got to compare it against the account that owns it.
        """
        data = account.extra_data or {}
        email = str(data.get('email') or '').strip()
        verified = data.get('email_verified', data.get('verified_email')) is True
        if account.provider != GOOGLE or not account.uid:
            self.reject(request, 'missing identity claim')
        if not email or not verified:
            self.reject(request, 'unverified email', email=email)
        linked = SocialAccount.objects.filter(provider=GOOGLE, uid=account.uid).first()
        if linked:
            stored = str((linked.extra_data or {}).get('email') or '').strip()
            if stored and stored.casefold() != email.casefold():
                self.reject(request, 'identity email mismatch', linked.user, email)
            if not linked.user.is_active:
                self.reject(request, 'inactive account', linked.user, email)
        return email

    def pre_social_login(self, request, sociallogin):
        email = self.validate_claims(request, sociallogin.account)
        if sociallogin.state.get('process', 'login') != 'login':
            self.reject(request, 'invalid login process', email=email)
        try:
            with transaction.atomic():
                users = list(get_user_model().objects.select_for_update().filter(email__iexact=email)[:2])
                if len(users) != 1:
                    # Reject outside the transaction so the audit is not rolled back.
                    raise Rejected('no matching account' if not users else 'ambiguous email', email=email)
                user = users[0]
                access = UserAccess.objects.select_for_update().filter(user=user, active=True).first()
                if not user.is_active or not access or access.role not in ROLES:
                    raise Rejected('inactive account', user=user, email=email)
                if request.user.is_authenticated and request.user.pk != user.pk:
                    raise Rejected('session account mismatch', user=user, email=email)
                identity = SocialAccount.objects.select_for_update().filter(provider=GOOGLE, uid=sociallogin.account.uid).first()
                if identity and identity.user_id != user.pk:
                    raise Rejected('identity email mismatch', user=user, email=email)
                if EmailAddress.objects.filter(email__iexact=email).exclude(user=user).exists():
                    raise Rejected('email already linked', user=user, email=email)
                # Only a not-yet-linked identity is created here, so the unique
                # constraint is the final arbiter of a concurrent first link.
                newly_linked = identity is None
                if newly_linked:
                    # One Google identity per Duesdesk account. The row lock
                    # above serialises this against a concurrent first link.
                    if SocialAccount.objects.filter(provider=GOOGLE, user=user).exists():
                        raise Rejected('different Google identity already linked', user=user, email=email)
                    identity = SocialAccount(provider=GOOGLE, uid=sociallogin.account.uid, user=user)
                identity.extra_data = {'email': email.lower(), 'email_verified': True}
                identity.last_login = timezone.now()
                identity.save()
                address = EmailAddress.objects.filter(user=user, email__iexact=email).first()
                if address is None:
                    address = EmailAddress(user=user, email=email.lower())
                address.verified = True
                address.primary = not EmailAddress.objects.filter(user=user, primary=True).exclude(pk=address.pk).exists()
                address.save()
                if newly_linked:
                    record(request, 'google_account_linked', user)
                record(request, 'google_login_success', user)
        except Rejected as error:
            self.reject(request, error.reason, error.user, error.email)
        except IntegrityError:
            # A concurrent first link or a duplicate address lost the race.
            self.reject(request, 'identity conflict', email=email)
        # Only once the writes are committed. Signing in inside the transaction
        # would leave a live session if the commit itself failed.
        # Retain passwords, rotate the session/CSRF normally, and avoid all
        # allauth signup, confirmation, connection and notification views.
        login(request, user, backend=BACKEND)
        request.session['google_verified_email'] = email.lower()
        raise ImmediateHttpResponse(HttpResponseRedirect('/'))


class Rejected(Exception):
    def __init__(self, reason, user=None, email=''):
        self.reason = reason
        self.user = user
        self.email = email
