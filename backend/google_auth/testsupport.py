"""Test-only helpers for driving signup verification end to end.

Signup creates nothing until an address has been proven by a code emailed to it,
so a test that wants an account to exist has to do what a real secretary does:
submit the form, land on the verification page, then enter the code. Keeping that
sequence in one place stops each test from re-implementing (and subtly diverging
from) the real requests.
"""
import re

from django.core import mail
from django.test import override_settings

from .models import PendingSignup

# Storage stays local, hashing stays cheap, and locmem keeps the emailed code in
# memory rather than on a real transport.
CODE_SETTINGS = {
    'STORAGES': {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
    'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher'],
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

VERIFY_PAGE = '/signup/verify/'
SIGNUP_PAGE = '/signup/'
CODE = re.compile(r'\b(\d{6})\b')


class SignupTestMixin:
    """The whole signup journey, in the order a secretary walks it."""

    def start_signup(self, data, invite='', client=None):
        """Submit the signup form and land on the verification page."""
        client = client or self.client
        url = SIGNUP_PAGE + ('?invite=' + invite if invite else '')
        email = (data.get('email') or '').strip().lower()
        with override_settings(**CODE_SETTINGS):
            response = client.post(url, data)
        self.assertEqual(response.status_code, 302, getattr(response, 'content', b'')[:300])
        self.assertEqual(response.url, VERIFY_PAGE)
        # Kept so a test can prove a failed completion did not spend the code.
        # Scoped to this address, because a test may hold several pending
        # signups at once when it is checking that one address does not consume
        # another's budget.
        self.pending_code_hash = PendingSignup.objects.get(email=email).code_hash
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

    def enter_code(self, code=None, client=None, **extra):
        """Type the emailed code into the verification page.

        The same override the code was sent under is applied here. The stored
        hash records the algorithm that produced it, and Django's check_password
        answers False for a hash made by an algorithm that is no longer
        configured, so hashing the code with the cheap test hasher and then
        verifying it under the real one would silently read as a wrong code.
        """
        client = client or self.client
        code = code if code is not None else self.emailed_code()
        with override_settings(**CODE_SETTINGS):
            return client.post(VERIFY_PAGE, {'action': 'code', 'code': code},
                               REMOTE_ADDR=extra.pop('REMOTE_ADDR', '192.0.2.12'),
                               **extra)

    def signup_with_code(self, data, invite='', client=None, code=None):
        """Submit the form, then verify with the emailed code. The usual way to
        get an account."""
        client = client or self.client
        self.start_signup(data, invite, client=client)
        return self.enter_code(code=code, client=client)