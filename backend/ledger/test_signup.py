"""The two signup pages: create the account, then optionally brand it.

Step one is the only unauthenticated write path and is proved in
``google_auth.tests``; this module is about the pages themselves -- what a visitor
can see, what the branding step does, and the unrelated account-access repair that
lives in the same area.
"""
import json
from io import BytesIO, StringIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .models import AuditEvent, Organisation, UserAccess
from google_auth.testsupport import SignupTestMixin

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def signup_data(email='new-signup@example.com', username='new-signup',
                organization='New Organization',
                password='A-Very-Strong-Signup-Password!'):
    """The posted signup form, so every suite describes it the same way."""
    return {'organization_name': organization, 'username': username, 'email': email,
            'password1': password, 'password2': password}


def logo_file(color='#cc2244'):
    output = BytesIO()
    Image.new('RGB', (40, 40), color).save(output, format='PNG')
    return SimpleUploadedFile('logo.png', output.getvalue(), content_type='image/png')


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGES)
class SignupPageTests(SignupTestMixin, TestCase):
    def test_the_google_provider_is_gone_from_every_page_a_visitor_can_see(self):
        for path in ('/', '/login/', '/signup/', '/account/reset/'):
            page = self.client.get(path, follow=True)
            body = page.content.decode('utf-8', 'replace').lower()
            self.assertNotIn('accounts/google', body, f'{path} still links to Google')
            self.assertNotIn('google', body, f'{path} still mentions Google')

    def test_signup_page_asks_only_for_what_the_account_needs(self):
        page = self.client.get('/signup/')

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertContains(page, 'Create a secretary account')
        self.assertContains(page, 'name="organization_name"')
        # Branding is its own skippable step now; the palette and logo are not here.
        self.assertNotContains(page, 'name="primary"')
        self.assertNotContains(page, 'type="color"')
        self.assertNotContains(page, 'logo-upload')

    def test_an_invitation_card_carries_the_inviting_organization(self):
        org = Organisation.objects.create(name='Inviting Organization', primary='#112233')
        inviter = User.objects.create_user('inviter', email='inviter@example.com')
        UserAccess.objects.create(user=inviter, organization=org, role='secretary')
        from .organization_views import token_hash
        from .models import SecretaryInvite
        from datetime import timedelta
        SecretaryInvite.objects.create(organization=org, email='new-signup@example.com',
                                       created_by=inviter, token_hash=token_hash('raw-token'),
                                       expires_at=timezone.now() + timedelta(days=1))

        page = self.client.get('/signup/?invite=raw-token')

        self.assertContains(page, 'Join Inviting Organization')
        self.assertNotContains(page, 'name="organization_name"')

    def test_signup_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post('/signup/', signup_data())

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_home_explains_missing_access(self):
        user = User.objects.create_user('legacy-user', password='A-Very-Strong-Signup-Password!')
        self.client.force_login(user)

        response = self.client.get('/')

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'no active organization access', status_code=403)

    def test_assign_access_command_repairs_legacy_user(self):
        user = User.objects.create_user('legacy-user')
        output = StringIO()
        org = Organisation.objects.create(name='Legacy')

        call_command('assign_access', user.username, '--role', 'secretary',
                     '--organization-id', str(org.pk), stdout=output)

        self.assertEqual(UserAccess.objects.get(user=user).role, 'secretary')
        self.assertIn('Created secretary access', output.getvalue())


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGES)
class BrandingStepTests(SignupTestMixin, TestCase):
    def create_account(self):
        self.start_signup(signup_data())
        self.client.logout()
        self.client.force_login(User.objects.get(username='new-signup'))
        return Organisation.objects.get(name='New Organization')

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        response = self.client.get('/signup/branding/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/login/')

    def test_the_page_offers_name_colours_and_a_skip(self):
        self.create_account()

        page = self.client.get('/signup/branding/')

        self.assertContains(page, 'Brand your workspace')
        self.assertContains(page, 'name="name"')
        self.assertContains(page, 'name="primary"')
        self.assertContains(page, 'value="skip"')

    def test_skipping_finishes_onboarding_without_touching_the_branding(self):
        org = self.create_account()

        response = self.client.post('/signup/branding/', {'action': 'skip'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/overview/')
        org.refresh_from_db()
        self.assertFalse(org.onboarding_required)
        self.assertIsNotNone(org.onboarded_at)
        self.assertEqual(org.primary, '#214f43')

    def test_saving_the_palette_applies_it_and_finishes_onboarding(self):
        org = self.create_account()

        response = self.client.post('/signup/branding/', {
            'name': 'Renamed Workspace', 'primary': '#112233',
            'secondary': '#fefefe', 'accent': '#aaccee'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/overview/')
        org.refresh_from_db()
        self.assertEqual(org.name, 'Renamed Workspace')
        self.assertEqual(org.primary, '#112233')
        self.assertFalse(org.onboarding_required)
        event = AuditEvent.objects.get(action='organization.updated')
        self.assertEqual(event.organization, org)

    def test_an_uploaded_logo_survives_the_branding_step(self):
        org = self.create_account()
        token = self.client.post('/api/branding/preview/', {'logo': logo_file()}).json()['logo_token']

        self.client.post('/signup/branding/', {'name': org.name, 'logo_token': token})

        org.refresh_from_db()
        self.assertTrue(org.logo)
        self.assertFalse(org.onboarding_required)

    def test_an_invalid_colour_is_reported_and_does_not_finish_onboarding(self):
        org = self.create_account()

        response = self.client.post('/signup/branding/', {'name': org.name,
                                                          'primary': 'red;display:none'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'role="alert"', status_code=200)
        org.refresh_from_db()
        self.assertEqual(org.primary, '#214f43')
        self.assertTrue(org.onboarding_required)
        self.assertIsNone(org.onboarded_at)

    def test_the_step_is_not_reachable_once_onboarding_is_done(self):
        self.create_account()
        self.client.post('/signup/branding/', {'action': 'skip'})

        response = self.client.get('/signup/branding/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/overview/')

    def test_a_legacy_organization_never_sees_the_step(self):
        user = User.objects.create_user('legacy', password='A-Very-Strong-Signup-Password!')
        UserAccess.objects.create(user=user, organization=Organisation.objects.create(name='Legacy'),
                                  role='secretary')
        self.client.force_login(user)

        response = self.client.get('/signup/branding/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/overview/')
