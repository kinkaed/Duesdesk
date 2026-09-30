"""Test-only helpers for driving signup verification and Google end to end.

Signup now creates nothing until an address has been proven, so a test that
wants an account to exist has to do what a real secretary does: submit the form,
land on the verification page, then finish with either Google or the emailed
code. Keeping that sequence in one place stops each test from re-implementing
(and subtly diverging from) the real requests.

Both Google flows share one callback, so these helpers use the same URL for
both and rely on the session, exactly as the application does.
"""
import re
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from django.core import mail
from django.test import override_settings

from .models import PendingSignup

PROVIDERS = {'google': {'APP': {'client_id': 'test-client', 'secret': 'test-secret', 'key': ''},
    'SCOPE': ['openid', 'email', 'profile'], 'AUTH_PARAMS': {'access_type': 'online'}, 'OAUTH_PKCE_ENABLED': True}}
STATIC = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
          'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}

# Apply to any TestCase that touches Google or the emailed code: the provider is
# configured, storage stays local, and locmem keeps the code in memory.
GOOGLE_SETTINGS = {
    'GOOGLE_CLIENT_ID': 'test-client',
    'GOOGLE_CLIENT_SECRET': 'test-secret',
    'SOCIALACCOUNT_PROVIDERS': PROVIDERS,
    'STORAGES': STATIC,
    'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher'],
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

ENTRY = '/accounts/google/login/'
CALLBACK = '/accounts/google/login/callback/'
VERIFY_ENTRY = '/accounts/google/verify/'
VERIFY_PAGE = '/signup/verify/'
SIGNUP_PAGE = '/signup/'
CODE = re.compile(r'\b(\d{6})\b')


def mocked_google(claims):
    """Google's replies, without a network call or real credentials."""
    return (
        patch('allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.get_access_token_data',
              return_value={'access_token': 'not-stored', 'refresh_token': 'not-stored-either',
                            'id_token': 'mocked-id-token'}),
        patch('allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter._decode_id_token',
              return_value=claims),
    )


class GoogleTestMixin:
    def google_claims(self, address='secretary@example.com', **claims):
        base = {'sub': 'google-user-1', 'email': address, 'email_verified': True}
        base.update(claims)
        return base

    def state_from(self, response):
        self.assertEqual(response.status_code, 302, getattr(response, 'content', b'')[:300])
        self.assertEqual(urlparse(response.url).hostname, 'accounts.google.com')
        return parse_qs(urlparse(response.url).query)['state'][0]

    def google_callback(self, state, claims, client=None, **extra):
        """Finish the round trip with Google answering `claims`."""
        token, id_token = mocked_google(claims)
        extra.setdefault('REMOTE_ADDR', '192.0.2.12')
        with token, id_token:
            return (client or self.client).get(CALLBACK, {'state': state, 'code': 'mocked-code'},
                                               **extra)

    # ------------------------------------------------------------------ Google

    def begin(self, entry=ENTRY, client=None, **kwargs):
        """Start a Google round trip and return its state."""
        return self.state_from((client or self.client).get(entry, kwargs))

    def sign_in_with_google(self, address, uid='google-user-1', client=None, **claims):
        """Sign in an existing account with Google. Returns the callback response."""
        client = client or self.client
        with override_settings(**GOOGLE_SETTINGS):
            state = self.begin(client=client)
            return self.google_callback(state, self.google_claims(address, sub=uid, **claims),
                                        client=client)

    def verify_with_google(self, email=None, uid='google-signup-1', client=None, **claims):
        """Prove the pending signup in this session with Google.

        This is the whole Google half of verification: it creates the account
        only once the address matches, and it is not a sign-in.
        """
        client = client or self.client
        pending = self.pending()
        with override_settings(**GOOGLE_SETTINGS):
            state = self.state_from(client.post(VERIFY_ENTRY))
            return self.google_callback(
                state,
                self.google_claims(email or pending.email, sub=uid, **claims),
                client=client)

    # -------------------------------------------------------------- the record

    def start_signup(self, data, invite='', client=None):
        """Submit the signup form and land on the verification page."""
        client = client or self.client
        url = SIGNUP_PAGE + ('?invite=' + invite if invite else '')
        with override_settings(**GOOGLE_SETTINGS):
            response = client.post(url, data)
        self.assertEqual(response.status_code, 302, getattr(response, 'content', b'')[:300])
        self.assertEqual(response.url, VERIFY_PAGE)
        # Kept so a test can prove a failed completion did not spend the code.
        self.pending_code_hash = PendingSignup.objects.get().code_hash
        return response

    def pending(self, email=None):
        pending = PendingSignup.objects.get(email=(email or '').lower()) if email \
            else PendingSignup.objects.get()
        self.assertFalse(pending.expired())
        return pending

    def emailed_code(self, index=-1):
        """The digits from the most recent code email, so tests never guess."""
        self.assertTrue(mail.outbox, 'no verification code was emailed')
        found = CODE.findall(mail.outbox[index].body)
        self.assertTrue(found, f'no code in the email: {mail.outbox[index].body!r}')
        return found[0]

    # ------------------------------------------------------- the two full runs

    def signup_with_code(self, data, invite='', client=None, code=None):
        """Submit the form, then verify with the emailed code."""
        client = client or self.client
        self.start_signup(data, invite, client=client)
        code = code if code is not None else self.emailed_code()
        return client.post(VERIFY_PAGE, {'action': 'code', 'code': code},
                           REMOTE_ADDR='192.0.2.12')

    def signup_with_google(self, data, invite='', client=None, uid='google-signup-1', **claims):
        """Submit the form, then verify with Google. The usual way to get an account."""
        client = client or self.client
        self.start_signup(data, invite, client=client)
        response = self.verify_with_google(uid=uid, client=client, **claims)
        self.assertEqual(response.status_code, 302, getattr(response, 'content', b'')[:300])
        return response