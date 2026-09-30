from io import StringIO
from datetime import timedelta

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from axes.models import AccessAttempt
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import AuditEvent, UserAccess, Organisation
from google_auth.models import GoogleAuthRejection
from google_auth.testsupport import GoogleTestMixin
from google_auth.testsupport import GoogleTestMixin


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SignupTests(GoogleTestMixin, TestCase):
    password = 'A-Very-Strong-Signup-Password!'

    def data(self, email='new-signup@example.com'):
        return {'organization_name': 'New Organization', 'username': 'new-signup',
                'email': email, 'password1': self.password, 'password2': self.password}

    def stale_proof(self, email):
        """Age the current proof past its 15 minutes, the way time alone would.

        The test client builds a fresh SessionStore on every ``.session``
        access, so the store has to be held on to or the edit is overwritten.
        """
        session = self.client.session
        session['verified_signup_email'] = {'email': email,
            'at': (timezone.now() - timedelta(minutes=16)).isoformat()}
        session.save()

    def test_signup_creates_secretary_account_and_logs_in(self):
        page = self.client.get('/signup/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertContains(page, 'Create a secretary account')
        self.assertContains(page, 'secretary accounts only')
        self.assertContains(page, 'Register as a Secretary')
        self.assertContains(page, 'Verify email with Google')
        self.assertNotContains(page, 'Verified &#10003;')

        response = self.signup_with_google(self.data())

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        user = User.objects.get(username='new-signup')
        self.assertEqual(user.email, 'new-signup@example.com')
        self.assertTrue(user.check_password(self.password))
        self.assertNotEqual(user.password, self.password)
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))
        self.assertEqual(user.access.role, 'secretary')
        self.assertIsNone(user.access.member_id)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertFalse(AccessAttempt.objects.filter(username=user.username).exists())

    def test_google_is_required_before_the_account_exists(self):
        response = self.client.post('/signup/', self.data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verify this email address with Google')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertFalse(Organisation.objects.filter(name='New Organization').exists())
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_google_proof_only_applies_to_the_address_it_verified(self):
        self.verify_signup_email('new-signup@example.com')

        response = self.client.post('/signup/', self.data('someone-else@example.com'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verify this email address with Google')
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_google_proof_expires(self):
        self.verify_signup_email('new-signup@example.com')
        self.stale_proof('new-signup@example.com')

        response = self.client.post('/signup/', self.data())

        self.assertContains(response, 'has expired')
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_google_proof_is_single_use(self):
        self.assertEqual(self.signup_with_google(self.data()).status_code, 302)
        self.assertNotIn('verified_signup_email', self.client.session)

        # The proof was spent creating that account. It must not linger as a
        # general "already verified" state for whoever comes along next, so a
        # fresh visitor with a different address is still sent back to Google.
        visitor = Client()
        response = visitor.post('/signup/', {**self.data(), 'username': 'second',
                                             'email': 'second@example.com'})

        self.assertContains(response, 'Verify this email address with Google')
        self.assertFalse(User.objects.filter(username='second').exists())

    def test_signup_marks_the_google_address_verified_and_links_the_identity(self):
        self.assertEqual(self.signup_with_google(self.data(), uid='google-new-uid').status_code, 302)

        user = User.objects.get(username='new-signup')
        self.assertTrue(EmailAddress.objects.get(user=user, email='new-signup@example.com').verified)
        social = SocialAccount.objects.get(user=user, provider='google')
        self.assertEqual(social.uid, 'google-new-uid')
        self.assertNotIn('access_token', social.extra_data)
        self.assertNotIn('refresh_token', social.extra_data)
        self.assertIn('signup_email_verified_google',
                      [e.action for e in AuditEvent.objects.filter(actor=user)])

    def test_verified_signup_email_survives_a_rejected_form(self):
        self.verify_signup_email('new-signup@example.com')
        page = self.client.get('/signup/')

        self.assertContains(page, 'Verified &#10003;')
        self.assertNotContains(page, 'Verify email with Google')

        # The proof stays put so a typo does not mean another round trip.
        response = self.client.post('/signup/', {**self.data(), 'password2': 'Different-Password!'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verified &#10003;')
        self.assertIn('verified_signup_email', self.client.session)

    def test_a_proof_with_no_identity_behind_it_is_not_a_proof(self):
        # The identity is what gets linked to the new account. A proof without
        # one would create an account its owner could never open with Google.
        self.verify_signup_email('new-signup@example.com')
        session = self.client.session
        session.pop('signup_google_uid')
        session.save()

        response = self.client.post('/signup/', self.data())

        self.assertContains(response, 'Verify this email address with Google')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'missing identity claim')

    def test_google_proof_refusal_is_audited(self):
        self.verify_signup_email('new-signup@example.com')
        self.stale_proof('new-signup@example.com')

        self.client.post('/signup/', self.data())

        rejection = GoogleAuthRejection.objects.get(email='new-signup@example.com')
        self.assertEqual(rejection.flow, 'signup')
        self.assertEqual(rejection.reason, 'expired verification')

    def test_signup_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/signup/', {
            'organization_name': 'New Organization',
            'username': 'new-signup',
            'email': 'new-signup@example.com',
            'password1': self.password,
            'password2': self.password,
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_home_explains_missing_access(self):
        user = User.objects.create_user('legacy-user', password=self.password)
        self.client.force_login(user)

        response = self.client.get('/')

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'no active organization access', status_code=403)

    def test_assign_access_command_repairs_legacy_user(self):
        user = User.objects.create_user('legacy-user', password=self.password)
        output = StringIO()

        org=Organisation.objects.create(name='Legacy')
        call_command('assign_access', user.username, '--role', 'secretary', '--organization-id', str(org.pk), stdout=output)

        self.assertEqual(UserAccess.objects.get(user=user).role, 'secretary')
        self.assertIn('Created secretary access', output.getvalue())

    def test_signup_rejects_duplicate_email_and_mismatched_password(self):
        User.objects.create_user(username='existing', email='person@example.com', password=self.password)

        response = self.client.post('/signup/', {
            'organization_name': 'New Organization',
            'username': 'new-signup',
            'email': 'PERSON@example.com',
            'password1': self.password,
            'password2': self.password,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already uses this email')
        self.assertFalse(User.objects.filter(username='new-signup').exists())

        response = self.client.post('/signup/', {
            'organization_name': 'New Organization',
            'username': 'new-signup',
            'email': 'new-signup@example.com',
            'password1': self.password,
            'password2': 'Different-Password!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The two password fields didn')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
