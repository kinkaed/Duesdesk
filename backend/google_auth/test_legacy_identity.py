"""Accounts that were created by the removed external provider.

Duesdesk used to let someone sign in with an external identity instead of a
password. Removing that provider would have locked every such account out of its
own organization, because Django refuses to send a reset link to an account with
no usable password. The removal migration records one ``LegacyIdentity`` row per
affected account, and these tests prove the record is read where it matters:

* an account that never had a password can now set one;
* an account that already had a password gains nothing from the mechanism;
* a disabled account gains nothing;
* the grant is spent once, so it cannot be reused;
* the response never reveals which addresses were affected.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .forms import AccountRecoveryForm
from .models import LegacyIdentity

FASTER = override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')

RESET = '/account/reset/'


def reset_url(user):
    """The link the emailed message contains: the token URL, before Django
    redirects it to the page that holds the form."""
    token = default_token_generator.make_token(user)
    return f'/account/reset/{urlsafe_base64_encode(force_bytes(user.pk))}/{token}/'


@FASTER
class LegacyIdentityRecoveryTests(TestCase):
    def provider_account(self, email='founder@example.com', username='founder',
                         active=True, linked=True):
        """An account exactly as the removed provider left it: no usable password."""
        user = get_user_model().objects.create_user(username, email, password=None)
        user.is_active = active
        user.save(update_fields=['is_active'])
        self.assertFalse(user.has_usable_password())
        if linked:
            LegacyIdentity.objects.create(user=user, provider='google', email=email)
        return user

    def test_the_removal_leaves_the_account_with_no_usable_password(self):
        """The precondition, stated so the tests below cannot pass vacuously."""
        self.provider_account()
        self.assertFalse(get_user_model().objects.get(
            username='founder').has_usable_password())

    def test_a_recorded_account_is_offered_a_reset_link(self):
        self.provider_account()
        form = AccountRecoveryForm(data={'email': 'founder@example.com'})
        self.assertTrue(form.is_valid())

        self.client.post(RESET, {'email': 'founder@example.com'})

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('founder@example.com', mail.outbox[0].to)
        self.assertIn(reset_url(get_user_model().objects.get(username='founder')),
                      mail.outbox[0].body)

    def choose_password(self, user, password='a-brand-new-password-9', client=None):
        """Follow the emailed link, then post the new password to the form.

        Django splits this in two: the token URL redirects to a set-password page
        that keeps the token in the session, so the token never travels in a
        Referer header. Posting straight to the token URL only ever redirects.
        """
        client = client or self.client
        landing = client.get(reset_url(user))
        self.assertEqual(landing.status_code, 302, landing.content[:300])
        self.assertNotIn(default_token_generator.make_token(user), landing.url,
                         'the token must not survive into the form URL')
        return client.post(landing.url,
                           {'new_password1': password, 'new_password2': password})

    def test_the_reset_lets_them_choose_a_password_and_then_sign_in(self):
        user = self.provider_account()
        self.client.post(RESET, {'email': 'founder@example.com'})

        done = self.choose_password(user)

        self.assertEqual(done.status_code, 302, done.content[:300])
        user.refresh_from_db()
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password('a-brand-new-password-9'))
        # A reset does not sign anyone in: the new password is proved by using it.
        self.assertIsNone(self.client.session.get('_auth_user_id'))
        signed_in = self.client.post('/login/', {'username': user.username,
                                                 'password': 'a-brand-new-password-9'})
        self.assertEqual(signed_in.status_code, 302)
        self.assertEqual(self.client.session.get('_auth_user_id'), str(user.pk))

    def test_the_grant_is_spent_once_the_password_exists(self):
        """A spent grant must not linger as a permanent way round the rule."""
        user = self.provider_account()
        self.client.post(RESET, {'email': 'founder@example.com'})
        self.choose_password(user)

        self.assertFalse(LegacyIdentity.objects.filter(user=user).exists())
        # An ordinary password account stays eligible for an ordinary reset, but
        # only because it has a password: nothing about the removed provider is
        # still involved.
        form = AccountRecoveryForm(data={'email': 'founder@example.com'})
        form.is_valid()
        self.assertEqual([u.username for u in form.get_users('founder@example.com')],
                         ['founder'])
        self.assertEqual(LegacyIdentity.objects.count(), 0)

    def test_an_account_with_no_record_gains_nothing(self):
        """The default must still be the default for everyone else.

        An active account with no usable password and no record is not eligible,
        so the reset form yields nothing and no mail goes out.
        """
        self.provider_account(username='unrecorded', email='unrecorded@example.com',
                              linked=False)
        form = AccountRecoveryForm(data={'email': 'unrecorded@example.com'})
        form.is_valid()

        self.client.post(RESET, {'email': 'unrecorded@example.com'})

        self.assertEqual(mail.outbox, [],
                         'an unrecorded account must not receive a reset link')

    def test_a_disabled_account_is_not_granted_a_link(self):
        """A disabled account must not be revivable through a recovery path."""
        self.provider_account(email='former@example.com', username='former', active=False)
        form = AccountRecoveryForm(data={'email': 'former@example.com'})
        form.is_valid()

        self.client.post(RESET, {'email': 'former@example.com'})

        self.assertEqual(mail.outbox, [])

    def test_an_account_that_already_had_a_password_gains_nothing(self):
        get_user_model().objects.create_user('ordinary', 'ordinary@example.com',
                                            password='an-existing-password-1')
        form = AccountRecoveryForm(data={'email': 'ordinary@example.com'})
        form.is_valid()

        # Eligible, but only through Django's ordinary rule, not the record.
        self.assertEqual([u.username for u in form.get_users('ordinary@example.com')],
                         ['ordinary'])
        self.assertFalse(LegacyIdentity.objects.exists())

    def test_the_response_is_identical_whether_or_not_an_account_exists(self):
        """The form must not become a probe for which addresses are registered."""
        self.provider_account(email='known@example.com', username='known')
        known = self.client.post(RESET, {'email': 'known@example.com'})
        self.client.logout()
        unknown = self.client.post(RESET, {'email': 'stranger@example.com'})

        # Both answers are the same redirect to the same "check your email" page,
        # and the address is never echoed back into the response.
        self.assertEqual(known.status_code, 302)
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.url, unknown.url)
        self.assertEqual(known.url, '/account/reset/sent/')
        self.assertNotIn('known@example.com', known.content.decode())
        self.assertNotIn('stranger@example.com', unknown.content.decode())

    def test_the_record_survives_the_provider_being_gone(self):
        """The row carries the address it was created with, as evidence."""
        self.provider_account()
        record = LegacyIdentity.objects.get()
        self.assertEqual(record.provider, 'google')
        self.assertEqual(record.email, 'founder@example.com')