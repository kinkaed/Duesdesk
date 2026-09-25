from axes.models import AccessAttempt
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from .models import UserAccess


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SignupTests(TestCase):
    password = 'A-Very-Strong-Signup-Password!'

    def test_signup_creates_hashed_user_and_logs_in(self):
        page = self.client.get('/signup/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'csrfmiddlewaretoken')

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
        self.assertFalse(UserAccess.objects.filter(user=user).exists())
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
