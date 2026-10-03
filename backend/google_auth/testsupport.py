"""Test-only helpers for driving signup end to end.

Signup creates a usable account immediately, so a test that wants an account to
exist simply submits the form. Keeping the posted shape in one place stops each
test from re-implementing (and subtly diverging from) the real request.
"""
from django.test import override_settings

# Storage stays local and hashing stays cheap.
CODE_SETTINGS = {
    'STORAGES': {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
    'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher'],
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

SIGNUP_PAGE = '/signup/'
BRANDING_PAGE = '/signup/branding/'
PASSWORD = 'A-Very-Strong-Signup-Password!'


def signup_data(email='new-signup@example.com', username='new-signup',
                organization='New Organization', password=PASSWORD):
    """The posted signup form, so every suite describes it the same way."""
    return {'organization_name': organization, 'username': username, 'email': email,
            'password1': password, 'password2': password}


class SignupTestMixin:
    """The whole signup journey, in the order a secretary walks it."""

    def start_signup(self, data, invite='', client=None, **extra):
        """Submit the signup form. The account exists when this returns."""
        client = client or self.client
        url = SIGNUP_PAGE + ('?invite=' + invite if invite else '')
        with override_settings(**CODE_SETTINGS):
            return client.post(url, data, **extra)

    def signup_with_code(self, data, invite='', client=None, **extra):
        """Kept under its former name for callers written before signup was
        immediate: submitting the form is now the whole journey."""
        return self.start_signup(data, invite, client=client, **extra)
