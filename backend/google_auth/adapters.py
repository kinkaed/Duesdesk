"""Google may prove an address or open an existing account. It creates neither."""
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
from . import flows
from .audit import record

GOOGLE = 'google'
ROLES = ('secretary', 'auditor')
BACKEND = 'django.contrib.auth.backends.ModelBackend'


class AccountAdapter(DefaultAccountAdapter):
    """Only an address just proven to Google is treated as verified."""

    def is_email_verified(self, request, email):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated or not email:
            return False
        user_email = flows.clean(user.email)
        if not flows.same(user_email, email):
            return False
        if not flows.same(request.session.get(flows.LOGGED_IN_EMAIL), user_email):
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
        """Refuse, and record the refusal against the flow that started it."""
        from .views import login_error, signup_error
        if flows.flow_of(request) == flows.SIGNUP:
            raise ImmediateHttpResponse(signup_error(request, reason, email))
        raise ImmediateHttpResponse(login_error(request, reason, user, email))

    def validate_claims(self, request, account):
        """Refuse a claim before allauth touches the database.

        Runs from the provider's ``complete_login``, which precedes
        ``SocialLogin.lookup()``. Without this, allauth would overwrite the
        stored ``extra_data`` of an already-linked Google identity before we
        ever got to compare it against the account that owns it.
        """
        flow = flows.flow_of(request)
        data = account.extra_data or {}
        # Matching is case-insensitive, but the refusal record keeps Google's
        # own spelling so the row still reads like what the provider claimed.
        claimed = flows.clean(data.get('email'))
        email = flows.normalize(claimed)
        verified = data.get('email_verified', data.get('verified_email')) is True
        if account.provider != GOOGLE or not account.uid:
            self.reject(request, 'missing identity claim', email=claimed)
        if not email or not verified:
            self.reject(request, 'unverified email', email=claimed)
        linked = SocialAccount.objects.filter(provider=GOOGLE, uid=account.uid).first()
        if flow == flows.SIGNUP:
            # Proving an address is not the same as joining with it. An address
            # that is already spoken for, or an identity already in use, is
            # refused here so that nothing at all is created.
            if get_user_model().objects.filter(email__iexact=email).exists():
                self.reject(request, 'account already exists', email=claimed)
            if linked:
                self.reject(request, 'identity already linked', email=claimed)
            return claimed
        if linked:
            stored = flows.normalize((linked.extra_data or {}).get('email'))
            if stored and stored != email:
                self.reject(request, 'identity email mismatch', linked.user, claimed)
            if not linked.user.is_active:
                self.reject(request, 'inactive account', linked.user, claimed)
        return claimed

    def pre_social_login(self, request, sociallogin):
        flow = flows.flow_of(request)
        if flow == flows.SIGNUP:
            self.verify_signup_email(request, sociallogin)
        elif flow == flows.LOGIN:
            self.sign_in(request, sociallogin)
        else:
            self.reject(request, 'unknown flow', email='')

    def verify_signup_email(self, request, sociallogin):
        """Mark the address typed into the signup form as proven.

        This signs nobody in and creates nothing. The account and the
        organization are still created by the ordinary signup submission,
        which refuses to proceed without the proof this sets.
        """
        claimed = self.validate_claims(request, sociallogin.account)
        email = flows.normalize(claimed)
        if sociallogin.state.get('process', 'login') != 'login':
            self.reject(request, 'invalid login process', email=claimed)
        typed = flows.normalize(request.session.get(flows.PENDING))
        if not typed:
            self.reject(request, 'no signup email', email=claimed)
        if typed != email:
            # The address being proven is not the one being registered.
            self.reject(request, 'email mismatch', email=claimed)
        request.session[flows.SIGNUP_UID] = sociallogin.account.uid
        flows.grant(request, email)
        flows.finish(request, flows.SIGNUP)
        raise ImmediateHttpResponse(HttpResponseRedirect('/signup/'))

    def sign_in(self, request, sociallogin):
        """Open an existing account whose address Google has verified."""
        claimed = self.validate_claims(request, sociallogin.account)
        email = flows.normalize(claimed)
        if sociallogin.state.get('process', 'login') != 'login':
            self.reject(request, 'invalid login process', email=claimed)
        try:
            with transaction.atomic():
                users = list(get_user_model().objects.select_for_update().filter(email__iexact=email)[:2])
                if len(users) != 1:
                    # Reject outside the transaction so the audit is not rolled back.
                    raise Rejected('no matching account' if not users else 'ambiguous email', email=claimed)
                user = users[0]
                access = UserAccess.objects.select_for_update().filter(user=user, active=True).first()
                if not user.is_active or not access or access.role not in ROLES:
                    raise Rejected('inactive account', user=user, email=claimed)
                if request.user.is_authenticated and request.user.pk != user.pk:
                    raise Rejected('session account mismatch', user=user, email=claimed)
                identity = SocialAccount.objects.select_for_update().filter(provider=GOOGLE, uid=sociallogin.account.uid).first()
                if identity and identity.user_id != user.pk:
                    raise Rejected('identity email mismatch', user=user, email=claimed)
                if EmailAddress.objects.filter(email__iexact=email).exclude(user=user).exists():
                    raise Rejected('email already linked', user=user, email=claimed)
                # Only a not-yet-linked identity is created here, so the unique
                # constraint is the final arbiter of a concurrent first link.
                newly_linked = identity is None
                if newly_linked:
                    # One Google identity per Duesdesk account. The row lock
                    # above serialises this against a concurrent first link.
                    if SocialAccount.objects.filter(provider=GOOGLE, user=user).exists():
                        raise Rejected('different Google identity already linked', user=user, email=claimed)
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
                    record(request, 'google_account_linked', user, email=email, flow=flows.LOGIN)
                record(request, 'google_login_success', user, email=email, flow=flows.LOGIN)
        except Rejected as error:
            self.reject(request, error.reason, error.user, error.email)
        except IntegrityError:
            # A concurrent first link or a duplicate address lost the race.
            self.reject(request, 'identity conflict', email=claimed)
        # Only once the writes are committed. Signing in inside the transaction
        # would leave a live session if the commit itself failed.
        # Retain passwords, rotate the session/CSRF normally, and avoid all
        # allauth signup, confirmation, connection and notification views.
        login(request, user, backend=BACKEND)
        request.session[flows.LOGGED_IN_EMAIL] = email.lower()
        raise ImmediateHttpResponse(HttpResponseRedirect('/'))


class Rejected(Exception):
    def __init__(self, reason, user=None, email=''):
        self.reason = reason
        self.user = user
        self.email = email
