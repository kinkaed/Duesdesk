"""Test-only helpers for driving the Google flows end to end.

Signup now requires a Google-verified address, so tests that want an account to
exist have to do the same thing a real secretary does: press the Google button,
then finish the round trip. Keeping that in one place stops each test from
re-implementing (and subtly diverging from) the real request sequence.
"""
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from django.test import override_settings

PROVIDERS = {'google': {'APP': {'client_id': 'test-client', 'secret': 'test-secret', 'key': ''},
    'SCOPE': ['openid', 'email', 'profile'], 'AUTH_PARAMS': {'access_type': 'online'}, 'OAUTH_PKCE_ENABLED': True}}
STATIC = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
          'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}

# Apply to any TestCase that touches Google: the provider is configured, storage
# stays local, and the locmem backend keeps reset emails in memory.
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
SIGNUP_ENTRY = '/accounts/google/signup-verify/'
SIGNUP_CALLBACK = '/accounts/google/signup-verify/callback/'


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
    def google_claims(self, email='secretary@example.com', **claims):
        base = {'sub': 'google-user-1', 'email': email, 'email_verified': True}
        base.update(claims)
        return base

    def state_from(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response.url).hostname, 'accounts.google.com')
        return parse_qs(urlparse(response.url).query)['state'][0]

    def google_callback(self, state, claims, callback=CALLBACK, client=None, **extra):
        """Finish the round trip with Google answering `claims`."""
        token, id_token = mocked_google(claims)
        extra.setdefault('REMOTE_ADDR', '192.0.2.12')
        with token, id_token:
            return (client or self.client).get(callback, {'state': state, 'code': 'mocked-code'}, **extra)

    def begin(self, entry=ENTRY, **kwargs):
        """Start the sign-in flow at Google and return its state."""
        return self.state_from(self.client.get(entry, kwargs))

    def finish(self, state, data=None, **extra):
        claims = self.google_claims()
        if data:
            claims.update(data)
        return self.google_callback(state, claims, **extra)

    def verify_signup_email(self, email, invite='', uid='google-signup-1', client=None, **claims):
        """Prove `email` through the signup flow. Returns the callback response.

        This is the whole Google half of signup: it must not create anything.
        """
        client = client or self.client
        # The settings live here so any test can reach the flow without having to
        # decorate itself; local credentials never touch a real provider.
        with override_settings(**GOOGLE_SETTINGS):
            entry = client.post(SIGNUP_ENTRY, {'email': email, 'invite': invite})
            state = self.state_from(entry)
            return self.google_callback(state, self.google_claims(email, sub=uid, **claims),
                                        callback=SIGNUP_CALLBACK, client=client)

    def signup_with_google(self, data, email=None, invite='', client=None, uid='google-signup-1', **claims):
        """Verify then sign up, which is what a real secretary now has to do.

        The invitation rides in the query string because that is where the
        signup view reads it from, not out of the posted form.
        """
        email = email or data['email']
        client = client or self.client
        proved = self.verify_signup_email(email, invite, uid, client=client, **claims)
        self.assertEqual(proved.status_code, 302, getattr(proved, 'content', b'')[:300])
        url = '/signup/?invite=' + invite if invite else '/signup/'
        return client.post(url, data)
