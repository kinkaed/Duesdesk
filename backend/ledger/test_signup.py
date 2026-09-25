from io import StringIO

from axes.models import AccessAttempt
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from .models import UserAccess


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SignupTests(TestCase):
    password = 'A-Very-Strong-Signup-Password!'

    def test_signup_creates_secretary_account_and_logs_in(self):
        page = self.client.get('/signup/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertContains(page, 'Create a secretary account')
        self.assertContains(page, 'secretary accounts only')
        self.assertContains(page, 'Register as a Secretary')

        response = self.client.post('/signup/', {
            'username': 'new-signup',
            'email': 'new-signup@example.com',
            'password1': self.password,
            'password2': self.password,
        })

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

    def test_signup_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/signup/', {
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
        self.assertContains(response, 'assign your account role', status_code=403)

    def test_assign_access_command_repairs_legacy_user(self):
        user = User.objects.create_user('legacy-user', password=self.password)
        output = StringIO()

        call_command('assign_access', user.username, '--role', 'secretary', stdout=output)

        self.assertEqual(UserAccess.objects.get(user=user).role, 'secretary')
        self.assertIn('Created secretary access', output.getvalue())

    def test_signup_rejects_duplicate_email_and_mismatched_password(self):
        User.objects.create_user(username='existing', email='person@example.com', password=self.password)

        response = self.client.post('/signup/', {
            'username': 'new-signup',
            'email': 'PERSON@example.com',
            'password1': self.password,
            'password2': self.password,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already uses this email')
        self.assertFalse(User.objects.filter(username='new-signup').exists())

        response = self.client.post('/signup/', {
            'username': 'new-signup',
            'email': 'new-signup@example.com',
            'password1': self.password,
            'password2': 'Different-Password!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The two password fields didn')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
