from io import BytesIO, StringIO
from datetime import timedelta

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from axes.models import AccessAttempt
from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .models import AuditEvent, Organisation, SecretaryInvite, UserAccess
from .organization_views import token_hash
from google_auth.models import GoogleAuthRejection, PendingSignup
from google_auth.testsupport import GOOGLE_SETTINGS, GoogleTestMixin


def signup_data(email='new-signup@example.com', username='new-signup',
                organization='New Organization',
                password='A-Very-Strong-Signup-Password!'):
    """The posted signup form, so every suite describes it the same way."""
    return {'organization_name': organization, 'username': username, 'email': email,
            'password1': password, 'password2': password}


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SignupTests(GoogleTestMixin, TestCase):
    password = 'A-Very-Strong-Signup-Password!'

    def data(self, email='new-signup@example.com'):
        return {'organization_name': 'New Organization', 'username': 'new-signup',
                'email': email, 'password1': self.password, 'password2': self.password}

    def age_the_pending(self, **fields):
        """Push a pending signup or its code into the past, the way time would.

        The test client builds a fresh SessionStore on every ``.session``
        access, so the store has to be held on to or the edit is overwritten.
        """
        pending = self.pending()
        for field, value in fields.items():
            setattr(pending, field, value)
        pending.save()
        return pending

    # ------------------------------------------------------- the form itself

    def test_signup_page_is_unchanged_and_has_no_google_control(self):
        page = self.client.get('/signup/')

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertContains(page, 'Create a secretary account')
        self.assertContains(page, 'secretary accounts only')
        self.assertContains(page, 'Register as a Secretary')
        self.assertContains(page, 'name="organization_name"')
        self.assertNotContains(page, 'accounts/google')

    def test_submitting_the_form_creates_nothing(self):
        response = self.client.post('/signup/', self.data())

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, '/signup/verify/', fetch_redirect_response=False)
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertFalse(Organisation.objects.filter(name='New Organization').exists())
        self.assertFalse(UserAccess.objects.exists())
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_submission_stores_the_details_but_never_the_password(self):
        self.client.post('/signup/', self.data())

        pending = self.pending()
        self.assertEqual(pending.email, 'new-signup@example.com')
        self.assertEqual(pending.username, 'new-signup')
        self.assertEqual(pending.organization_name, 'New Organization')
        self.assertTrue(pending.password_hash.startswith('md5$'))
        self.assertNotIn(self.password, pending.password_hash)
        self.assertNotIn(self.password, repr(pending.__dict__))
        # The code that was emailed is a hash too, never the digits.
        self.assertNotEqual(pending.code_hash, self.emailed_code())

    def test_email_is_stored_lowercased(self):
        self.client.post('/signup/', self.data('Mixed.Case@Example.com'))

        self.assertEqual(self.pending().email, 'mixed.case@example.com')

    def test_pending_signup_holds_no_organization(self):
        self.client.post('/signup/', self.data())

        pending = self.pending()
        self.assertEqual([f.name for f in pending._meta.fields if f.is_relation], [])
        self.assertEqual([f.name for f in pending._meta.fields if f.remote_field], [])

    def test_latest_submission_wins_for_one_address(self):
        self.client.post('/signup/', self.data())
        self.client.post('/signup/', self.data('new-signup@example.com'))

        self.assertEqual(PendingSignup.objects.count(), 1)
        self.assertEqual(PendingSignup.objects.get().username, 'new-signup')

    def test_existing_email_is_refused_with_the_exact_message(self):
        User.objects.create_user(username='existing', email='person@example.com',
                                 password=self.password)

        response = self.client.post('/signup/', self.data('PERSON@example.com'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'An account with this email already exists, please log in.')
        self.assertFalse(PendingSignup.objects.filter(email='person@example.com').exists())
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_mismatched_passwords_create_nothing(self):
        response = self.client.post('/signup/', {
            'organization_name': 'New Organization', 'username': 'new-signup',
            'email': 'new-signup@example.com', 'password1': self.password,
            'password2': 'Different-Password!'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The two password fields didn')
        self.assertFalse(PendingSignup.objects.exists())

    def test_missing_organization_name_creates_nothing(self):
        data = self.data()
        del data['organization_name']

        response = self.client.post('/signup/', data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter an organization name.')
        self.assertFalse(PendingSignup.objects.exists())

    def test_signup_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post('/signup/', self.data())

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PendingSignup.objects.exists())
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    # -------------------------------------------------------- the code path

    def test_a_code_is_emailed_and_is_not_stored_in_the_clear(self):
        with override_settings(**GOOGLE_SETTINGS):
            self.client.post('/signup/', self.data())

        self.assertEqual(len(mail.outbox), 1)
        code = self.emailed_code()
        self.assertRegex(code, r'^\d{6}$')
        self.assertIn('10 minutes', mail.outbox[0].body)
        pending = self.pending()
        self.assertNotEqual(pending.code_hash, code)
        self.assertNotIn(code, pending.code_hash)
        self.assertTrue(pending.code_live())

    def test_the_right_code_creates_the_account_and_signs_in(self):
        with override_settings(**GOOGLE_SETTINGS):
            response = self.signup_with_code(self.data())

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        user = User.objects.get(username='new-signup')
        self.assertEqual(user.email, 'new-signup@example.com')
        self.assertTrue(user.check_password(self.password))
        self.assertNotEqual(user.password, self.password)
        self.assertEqual(user.access.role, 'secretary')
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))
        self.assertTrue(Organisation.objects.filter(name='New Organization').exists())
        self.assertFalse(PendingSignup.objects.exists())
        self.assertFalse(AccessAttempt.objects.filter(username=user.username).exists())

    def test_the_code_is_single_use(self):
        with override_settings(**GOOGLE_SETTINGS):
            self.signup_with_code(self.data())

        self.assertFalse(PendingSignup.objects.exists())
        self.assertEqual(User.objects.count(), 1)

    def test_a_wrong_code_creates_nothing_and_is_counted(self):
        self.client.post('/signup/', self.data())

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post('/signup/verify/', {'action': 'code', 'code': 'not-six-digits'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'That code is not correct')
        self.assertEqual(self.pending().code_attempts, 1)
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertFalse(Organisation.objects.filter(name='New Organization').exists())

    def test_five_wrong_attempts_invalidate_the_code(self):
        self.client.post('/signup/', self.data())

        with override_settings(**GOOGLE_SETTINGS):
            for _ in range(5):
                self.client.post('/signup/verify/', {'action': 'code', 'code': 'not-six-digits'})
            response = self.client.post('/signup/verify/', {'action': 'code', 'code': 'not-six-digits'})

        self.assertContains(response, 'Too many incorrect attempts', status_code=200)
        pending = self.pending()
        self.assertTrue(pending.code_dead)
        self.assertEqual(pending.code_hash, '')
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_the_right_code_still_works_before_the_fifth_wrong_try(self):
        self.client.post('/signup/', self.data())
        code = self.emailed_code()

        with override_settings(**GOOGLE_SETTINGS):
            for _ in range(4):
                self.client.post('/signup/verify/', {'action': 'code', 'code': 'not-six-digits'})
            response = self.client.post('/signup/verify/', {'action': 'code', 'code': code})

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username='new-signup').exists())

    def test_an_expired_code_is_refused(self):
        self.client.post('/signup/', self.data())
        self.age_the_pending(code_expires_at=timezone.now() - timedelta(seconds=1))

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post('/signup/verify/',
                                        {'action': 'code', 'code': self.emailed_code()})

        self.assertContains(response, 'has expired', status_code=200)
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_an_expired_pending_signup_cannot_be_verified(self):
        self.client.post('/signup/', self.data())
        self.age_the_pending(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.client.get('/signup/verify/')

        self.assertEqual(response.status_code, 410)
        self.assertContains(response, 'This sign-up has expired', status_code=410)
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_resend_is_held_back_by_the_cooldown(self):
        self.client.post('/signup/', self.data())

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post('/signup/verify/', {'action': 'resend'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please wait a moment')
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_is_allowed_once_the_cooldown_passes(self):
        self.client.post('/signup/', self.data())
        self.age_the_pending(code_sends=[
            (timezone.now() - timedelta(seconds=61)).isoformat()])

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post('/signup/verify/', {'action': 'resend'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(len(self.pending().code_sends), 2)

    def test_resend_stops_after_five_in_an_hour(self):
        self.client.post('/signup/', self.data())
        self.age_the_pending(code_sends=[
            (timezone.now() - timedelta(seconds=61 + i * 600)).isoformat() for i in range(5)])

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post('/signup/verify/', {'action': 'resend'})

        self.assertContains(response, 'Too many codes requested', status_code=200)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_verification_page_offers_both_ways_to_prove_the_address(self):
        self.client.post('/signup/', self.data())

        page = self.client.get('/signup/verify/')

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'new-signup@example.com')
        self.assertContains(page, 'Continue with Google')
        self.assertContains(page, 'Email me a code')
        self.assertContains(page, 'Wrong email? Go back')
        self.assertContains(page, 'name="code"')

    def test_going_back_keeps_the_typed_values_but_not_the_password(self):
        self.client.post('/signup/', self.data())
        pending = self.pending()

        response = self.client.post('/signup/verify/', {'action': 'back'})

        self.assertRedirects(response, '/signup/', fetch_redirect_response=False)
        session = self.client.session
        self.assertNotIn(self.password, str(session))
        self.assertEqual(session['signup_return']['organization_name'], 'New Organization')

    def test_the_back_button_is_prefilled_from_the_pending_signup(self):
        # Submitted values are read back out of the pending signup, so nothing
        # has to be carried through a hidden field or a query string.
        self.client.post('/signup/', {**self.data(), 'primary': '#112233',
                                      'secondary': '#fefefe', 'accent': '#aaccee'})
        pending = self.pending()
        self.assertEqual(pending.palette['primary'], '#112233')

        self.client.post('/signup/verify/', {'action': 'back'})
        page = self.client.get('/signup/')

        self.assertContains(page, 'value="new-signup"')
        self.assertContains(page, 'value="new-signup@example.com"')
        self.assertContains(page, 'value="New Organization"')
        self.assertContains(page, 'value="#112233"')
        self.assertContains(page, 'value="#aaccee"')

    def test_the_logo_survives_going_back(self):
        # The preview token is signed for this session and expires in half an
        # hour, so it is held in the session to put the logo back on the form.
        output = BytesIO()
        Image.new('RGB', (40, 40), '#cc2244').save(output, format='PNG')
        preview = self.client.post('/api/branding/preview/', {
            'logo': SimpleUploadedFile('logo.png', output.getvalue(), content_type='image/png')})
        token = preview.json()['logo_token']

        self.client.post('/signup/', {**self.data(), 'logo_token': token,
                                      'primary': '#112233'})
        self.assertTrue(PendingSignup.objects.get().logo)

        self.client.post('/signup/verify/', {'action': 'back'})
        page = self.client.get('/signup/')

        self.assertContains(page, 'value="%s"' % token)
        self.assertContains(page, 'value="#112233"')

    def test_the_invitation_survives_going_back(self):
        # Only the invitation's hash is stored with the pending signup, so the
        # raw token is held in the session to put the back form back on it.
        organization = Organisation.objects.create(name='Inviting Organization')
        raw = 'an-invitation-token'
        inviter = User.objects.create_user('inviter', email='inviter@example.com')
        UserAccess.objects.create(user=inviter, organization=organization, role='secretary')
        SecretaryInvite.objects.create(
            organization=organization, email='new-signup@example.com', created_by=inviter,
            token_hash=token_hash(raw), expires_at=timezone.now() + timedelta(days=1))

        self.client.post('/signup/?invite=' + raw, self.data())
        self.assertTrue(PendingSignup.objects.get().invite_token_hash)

        self.client.post('/signup/verify/', {'action': 'back'})
        page = self.client.get('/signup/')

        # The form still knows it is joining somebody else's organization, and
        # finishing it again consumes the same invitation.
        self.assertContains(page, 'Join Inviting Organization')
        self.client.post('/signup/', self.data())
        self.client.post('/signup/verify/', {'action': 'code', 'code': self.emailed_code()})

        user = User.objects.get(username='new-signup')
        self.assertEqual(user.access.organization.name, 'Inviting Organization')
        self.assertEqual(SecretaryInvite.objects.get().used_by, user)

    def test_coming_back_repopulates_the_form(self):
        self.client.post('/signup/', self.data())
        self.client.post('/signup/verify/', {'action': 'back', 'username': 'new-signup',
                                             'email': 'new-signup@example.com',
                                             'organization_name': 'New Organization'})

        page = self.client.get('/signup/')

        self.assertContains(page, 'value="new-signup"')
        self.assertContains(page, 'value="new-signup@example.com"')
        self.assertContains(page, 'value="New Organization"')

    def test_one_browser_cannot_enumerate_pending_signups(self):
        self.client.post('/signup/', self.data())

        # A visitor with no session of their own sees nothing at all, and the
        # address is never in the URL to be guessed or shared.
        stranger = Client()
        response = stranger.get('/signup/verify/')

        self.assertEqual(response.status_code, 410)
        self.assertNotContains(response, 'new-signup@example.com', status_code=410)
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    # -------------------------------------------------------------- auditing

    def test_the_address_is_marked_verified_and_audited(self):
        self.signup_with_code(self.data())

        user = User.objects.get(username='new-signup')
        self.assertTrue(EmailAddress.objects.get(
            user=user, email='new-signup@example.com').verified)
        self.assertIn('signup_email_verified',
                      [e.action for e in AuditEvent.objects.filter(actor=user)])
        details = next(e.details for e in AuditEvent.objects.filter(
            actor=user, action='signup_email_verified'))
        self.assertIn('"method": "code"', details)
        self.assertIn('192.0.2.12', details)

    # ------------------------------------------------------- unrelated, kept

    def test_home_explains_missing_access(self):
        user = User.objects.create_user('legacy-user', password=self.password)
        self.client.force_login(user)

        response = self.client.get('/')

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'no active organization access', status_code=403)

    def test_assign_access_command_repairs_legacy_user(self):
        user = User.objects.create_user('legacy-user', password=self.password)
        output = StringIO()

        org = Organisation.objects.create(name='Legacy')
        call_command('assign_access', user.username, '--role', 'secretary',
                     '--organization-id', str(org.pk), stdout=output)

        self.assertEqual(UserAccess.objects.get(user=user).role, 'secretary')
        self.assertIn('Created secretary access', output.getvalue())

    def test_cleanup_command_removes_expired_rows_only(self):
        self.client.post('/signup/', self.data())
        fresh = self.pending()
        expired = PendingSignup.objects.create(
            email='gone@example.com', username='gone', organization_name='Gone Co',
            password_hash='x', expires_at=timezone.now() - timedelta(seconds=1))

        output = StringIO()
        call_command('cleanup_pending_signups', stdout=output)

        self.assertIn('Deleted 1 row(s)', output.getvalue())
        self.assertTrue(PendingSignup.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(PendingSignup.objects.filter(pk=expired.pk).exists())


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ExistingEmailTests(TestCase):
    """The refusal when the address already belongs to an account."""

    def test_it_never_creates_a_pending_signup(self):
        User.objects.create_user(username='existing', email='person@example.com',
                                 password='A-Very-Strong-Signup-Password!')

        response = self.client.post('/signup/', {
            'organization_name': 'New Organization', 'username': 'new-signup',
            'email': 'person@example.com', 'password1': 'A-Very-Strong-Signup-Password!',
            'password2': 'A-Very-Strong-Signup-Password!'})

        self.assertContains(response,
                            'An account with this email already exists, please log in.')
        self.assertFalse(PendingSignup.objects.exists())
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'account already exists')