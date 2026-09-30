import json
import re
from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, Client, RequestFactory, override_settings
from django.utils import timezone
from allauth.account.adapter import get_adapter as get_account_adapter
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialToken
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from axes.handlers.proxy import AxesProxyHandler
from axes.models import AccessLog
from ledger.models import Organisation, UserAccess, Member, AuditEvent, SecretaryInvite
from .models import GoogleAuthRejection
from .testsupport import GOOGLE_SETTINGS, GoogleTestMixin

PROVIDERS = {'google': {'APP': {'client_id': 'test-client', 'secret': 'test-secret', 'key': ''},
    'SCOPE': ['openid', 'email', 'profile'], 'AUTH_PARAMS': {'access_type': 'online'}, 'OAUTH_PKCE_ENABLED': True}}
STATIC = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
          'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(GOOGLE_CLIENT_ID='test-client', GOOGLE_CLIENT_SECRET='test-secret',
    SOCIALACCOUNT_PROVIDERS=PROVIDERS, STORAGES=STATIC,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class GoogleAuthenticationTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Organization A')
        self.other_org = Organisation.objects.create(name='Organization B')
        self.user = get_user_model().objects.create_user('secretary', email='Secretary@example.com', password='ExistingPassword25!')
        self.other = get_user_model().objects.create_user('other', email='other@example.com', password='OtherPassword25!')
        UserAccess.objects.create(user=self.user, organization=self.org, role='secretary')
        UserAccess.objects.create(user=self.other, organization=self.other_org, role='secretary')
        self.member = Member.objects.create(organization=self.org, full_name='Member A', joined=date(2026, 1, 1))
        self.other_member = Member.objects.create(organization=self.other_org, full_name='Member B', joined=date(2026, 1, 1))
        self.initial_org_count = Organisation.objects.count()

    def begin(self, **kwargs):
        response = self.client.get('/accounts/google/login/', kwargs)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response.url).hostname, 'accounts.google.com')
        return parse_qs(urlparse(response.url).query)

    def finish(self, state, data=None, **extra):
        claims = {'sub': 'google-user-1', 'email': 'secretary@EXAMPLE.com', 'email_verified': True}
        if data:
            claims.update(data)
        extra.setdefault('REMOTE_ADDR', '192.0.2.12')
        with patch('allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.get_access_token_data',
                   return_value={'access_token': 'not-stored', 'refresh_token': 'not-stored-either', 'id_token': 'mocked-id-token'}), \
             patch('allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter._decode_id_token', return_value=claims):
            return self.client.get('/accounts/google/login/callback/', {'state': state, 'code': 'mocked-code'}, **extra)

    def authenticate(self, data=None):
        return self.finish(self.begin()['state'][0], data)

    def reset_link(self):
        match = re.search(r'https?://\S+/account/reset/\S+', mail.outbox[0].body)
        self.assertIsNotNone(match, mail.outbox[0].body if mail.outbox else 'no email sent')
        return match.group(0)

    def reset_uid(self):
        return self.reset_link().rsplit('/', 2)[1]

    def open_reset_form(self, client=None):
        """Follow the emailed link, including Django's token-stripping hop."""
        client = client or self.client
        hop = client.get(self.reset_link())
        self.assertEqual(hop.status_code, 302)
        form = client.get(urlparse(hop.url).path)
        self.assertEqual(form.status_code, 200)
        return form

    def test_verified_email_logs_in_without_password(self):
        response = self.authenticate()
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.assertEqual(self.client.session['google_verified_email'], self.user.email.lower())
        self.assertTrue(EmailAddress.objects.get(user=self.user).verified)

    def test_link_existing_account_preserves_password_and_no_tokens(self):
        old_hash = self.user.password
        self.authenticate()
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, old_hash)
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertEqual(Organisation.objects.count(), self.initial_org_count)
        self.assertEqual(SocialAccount.objects.get().user, self.user)
        self.assertEqual(SocialToken.objects.count(), 0)
        self.assertNotIn('not-stored', json.dumps(dict(self.client.session)))

    def test_unverified_email_is_rejected(self):
        response = self.authenticate({'email_verified': False})
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'registration/login.html')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(SocialAccount.objects.exists())

    def test_string_verification_flag_is_rejected(self):
        self.assertEqual(self.authenticate({'email_verified': 'false'}).status_code, 403)

    def test_unknown_email_does_not_create_any_account_or_organization(self):
        response = self.authenticate({'email': 'unknown@example.com'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertEqual(Organisation.objects.count(), self.initial_org_count)
        self.assertFalse(SocialAccount.objects.exists())
        self.assertFalse(EmailAddress.objects.exists())

    def test_duplicate_case_insensitive_email_is_rejected(self):
        get_user_model().objects.create_user('duplicate', email='SECRETARY@EXAMPLE.COM')
        self.assertEqual(self.authenticate().status_code, 403)
        self.assertFalse(SocialAccount.objects.exists())

    def test_linked_identity_cannot_switch_to_other_email_or_organization(self):
        self.authenticate()
        before = SocialAccount.objects.get().extra_data
        self.client.logout()
        response = self.authenticate({'email': self.other.email})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(SocialAccount.objects.get().user_id, self.user.pk)
        # allauth refreshes a matched SocialAccount during lookup(), before
        # pre_social_login runs. A refused claim must not have reached the row.
        self.assertEqual(SocialAccount.objects.get().extra_data, before)

    def test_user_without_membership_is_refused_and_owns_no_tenant(self):
        UserAccess.objects.filter(user=self.user).delete()
        self.assertEqual(self.authenticate().status_code, 403)
        self.assertFalse(SocialAccount.objects.exists())
        self.assertEqual(Organisation.objects.count(), self.initial_org_count)
        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.user, self.user)
        self.assertEqual(rejection.reason, 'inactive account')

    def test_second_google_identity_cannot_attach_to_one_account(self):
        self.authenticate()
        self.client.logout()
        response = self.authenticate({'sub': 'google-user-2'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SocialAccount.objects.count(), 1)
        self.assertEqual(SocialAccount.objects.get().user_id, self.user.pk)

    def test_concurrent_first_link_fails_closed(self):
        # A second tab racing the same first link loses to the unique
        # constraint; it must be refused, not silently attached twice.
        real_save = SocialAccount.save

        def race(self, *args, **kwargs):
            if self.pk is None:
                from django.db import IntegrityError
                raise IntegrityError('duplicate key')
            return real_save(self, *args, **kwargs)

        with patch.object(SocialAccount, 'save', race):
            self.assertEqual(self.authenticate().status_code, 403)
        self.assertFalse(SocialAccount.objects.exists())
        self.assertFalse(EmailAddress.objects.exists())
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(get_user_model().objects.filter(email=self.user.email).count(), 1)

    def test_logged_in_user_cannot_link_other_account(self):
        self.client.force_login(self.other)
        self.assertEqual(self.authenticate().status_code, 403)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.other.pk))
        self.assertFalse(SocialAccount.objects.exists())

    def test_inactive_user_and_inactive_membership_rejected(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.authenticate().status_code, 403)
        self.user.is_active = True
        self.user.save()
        UserAccess.objects.filter(user=self.user).update(active=False)
        self.assertEqual(self.authenticate().status_code, 403)

    def test_org_scoping_after_google_login(self):
        self.authenticate()
        result = self.client.get('/api/members/').json()
        self.assertEqual([member['id'] for member in result['members']], [self.member.pk])
        self.assertEqual(self.client.get(f'/api/members/{self.other_member.pk}/').status_code, 404)
        self.assertEqual(self.client.post(f'/api/accounts/{self.other.pk}/disable/').status_code, 404)

    def test_success_and_link_audit_records(self):
        self.authenticate()
        events = AuditEvent.objects.filter(action__startswith='google_')
        self.assertEqual(set(events.values_list('action', flat=True)), {'google_account_linked', 'google_login_success'})
        for event in events:
            self.assertEqual(event.actor, self.user)
            self.assertEqual(event.organization, self.org)
            self.assertEqual(json.loads(event.details)['ip'], '192.0.2.12')
            self.assertIsNotNone(event.created_at)
        self.client.logout()
        self.authenticate()
        self.assertEqual(AuditEvent.objects.filter(action='google_account_linked').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='google_login_success').count(), 2)

    def test_known_account_rejection_has_audit_reason(self):
        self.user.is_active = False
        self.user.save()
        self.authenticate()
        event = AuditEvent.objects.get(action='google_login_rejected')
        self.assertEqual(event.organization, self.org)
        self.assertEqual(json.loads(event.details)['reason'], 'inactive account')
        # A member's refusal is the member's organization's business only.
        self.assertFalse(GoogleAuthRejection.objects.exists())

    def test_unknown_email_rejection_is_persisted_without_any_tenant(self):
        self.assertEqual(self.authenticate({'email': 'unknown@example.com'}).status_code, 403)
        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.action, 'google_login_rejected')
        self.assertEqual(rejection.reason, 'no matching account')
        self.assertEqual(rejection.email, 'unknown@example.com')
        self.assertEqual(rejection.provider, 'google')
        self.assertIsNone(rejection.user)
        self.assertEqual(rejection.ip, '192.0.2.12')
        self.assertIsNotNone(rejection.created_at)
        self.assertFalse(AuditEvent.objects.filter(action__startswith='google_').exists())
        self.assertFalse(hasattr(rejection, 'organization'))
        self.assertNotIn('organization', [f.name for f in GoogleAuthRejection._meta.get_fields()])

    def test_unverified_email_rejection_is_persisted(self):
        self.assertEqual(self.authenticate({'email_verified': False}).status_code, 403)
        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.reason, 'unverified email')
        self.assertEqual(rejection.email, 'secretary@EXAMPLE.com')
        self.assertIsNone(rejection.user)

    def test_ambiguous_email_rejection_is_persisted_for_both_candidates(self):
        get_user_model().objects.create_user('duplicate', email='SECRETARY@EXAMPLE.COM')
        self.assertEqual(self.authenticate().status_code, 403)
        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.reason, 'ambiguous email')
        self.assertIsNone(rejection.user)

    @override_settings(GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET='')
    def test_unconfigured_provider_rejection_is_persisted(self):
        self.assertEqual(self.client.get('/accounts/google/login/').status_code, 403)
        self.assertEqual(self.client.get('/accounts/google/login/callback/').status_code, 403)
        self.assertEqual(GoogleAuthRejection.objects.filter(reason='provider not configured').count(), 2)
        self.assertEqual(GoogleAuthRejection.objects.filter(email='').count(), 2)

    def test_rejection_table_survives_user_deletion(self):
        UserAccess.objects.filter(user=self.user).delete()
        self.authenticate()
        self.user.delete()
        rejection = GoogleAuthRejection.objects.get()
        self.assertIsNone(rejection.user)
        self.assertEqual(rejection.email, 'secretary@EXAMPLE.com')

    def test_audit_write_failure_does_not_break_sign_in_or_refusal(self):
        with patch('google_auth.audit.AuditEvent.objects.create', side_effect=ValueError('audit down')):
            self.assertRedirects(self.authenticate(), '/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.client.logout()
        UserAccess.objects.filter(user=self.user).delete()
        with patch('google_auth.audit.GoogleAuthRejection.objects.create', side_effect=ValueError('audit down')):
            self.assertEqual(self.authenticate().status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_audit_ip_ignores_forwarded_headers(self):
        self.authenticate()
        event = AuditEvent.objects.get(action='google_login_success')
        self.assertEqual(json.loads(event.details)['ip'], '192.0.2.12')
        self.assertEqual(GoogleAuthRejection.objects.count(), 0)

    def test_audit_ip_is_the_server_observation_not_a_forwarded_header(self):
        self.finish(self.begin()['state'][0], {'email': 'unknown@example.com'},
            REMOTE_ADDR='198.51.100.9', HTTP_X_FORWARDED_FOR='203.0.113.7',
            HTTP_X_REAL_IP='203.0.113.7', HTTP_CF_CONNECTING_IP='203.0.113.7')
        self.assertEqual(GoogleAuthRejection.objects.get().ip, '198.51.100.9')

    def test_audit_tolerates_a_malformed_remote_addr(self):
        self.finish(self.begin()['state'][0], {'email': 'unknown@example.com'}, REMOTE_ADDR='not-an-ip')
        self.assertEqual(GoogleAuthRejection.objects.get().ip, '')

    def test_password_login_still_works(self):
        response = self.client.post('/login/', {'username': self.user.username, 'password': 'ExistingPassword25!'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))

    def test_email_cannot_replace_the_username_in_the_login_form(self):
        response = self.client.post('/login/', {'username': self.user.email, 'password': 'ExistingPassword25!'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_no_allauth_authentication_backend_is_registered(self):
        from django.conf import settings
        from django.contrib.auth import get_backends
        registered = [f'{type(b).__module__}.{type(b).__name__}' for b in get_backends()]
        self.assertEqual(registered, ['django.contrib.auth.backends.ModelBackend',
                                      'axes.backends.AxesStandaloneBackend'])
        self.assertNotIn('allauth', ' '.join(registered))
        self.assertEqual(settings.ACCOUNT_LOGIN_METHODS, {'username'})

    def test_password_lockout_cannot_be_bypassed_with_correct_password(self):
        for _ in range(5):
            self.client.post('/login/', {'username': self.user.username, 'password': 'wrong'})
        response = self.client.post('/login/', {'username': self.user.username, 'password': 'ExistingPassword25!'})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_google_only_user_can_use_existing_reset_flow(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()
        self.client.logout()
        response = self.client.post('/account/reset/', {'email': self.user.email})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/account/reset/', mail.outbox[0].body)

    def test_google_only_user_can_finish_the_reset_and_sign_in(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()
        self.client.logout()
        self.client.post('/account/reset/', {'email': self.user.email})
        confirm = self.open_reset_form()
        self.assertTemplateUsed(confirm, 'registration/account_form.html')
        self.assertNotIn('_auth_user_id', self.client.session)
        done = self.client.post(urlparse(confirm.request['PATH_INFO']).path, {
            'new_password1': 'FreshPassword26!', 'new_password2': 'FreshPassword26!'})
        self.assertEqual(done.status_code, 302)
        self.assertEqual(urlparse(done.url).path, '/account/reset/complete/')
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_usable_password())
        self.assertTrue(self.user.check_password('FreshPassword26!'))
        # The Google link survives the new password, and the new password works.
        self.client.post('/logout/')
        signed_in = self.client.post('/login/', {
            'username': self.user.username, 'password': 'FreshPassword26!'})
        self.assertEqual(signed_in.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.client.post('/logout/')
        self.assertRedirects(self.authenticate(), '/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.assertEqual(SocialAccount.objects.get().user_id, self.user.pk)

    def test_reset_token_for_google_user_cannot_be_reused(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()
        self.client.logout()
        self.client.post('/account/reset/', {'email': self.user.email})
        confirm = self.open_reset_form()
        self.client.post(urlparse(confirm.request['PATH_INFO']).path, {
            'new_password1': 'FreshPassword26!', 'new_password2': 'FreshPassword26!'})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('FreshPassword26!'))
        stolen = Client()
        stolen.get(self.reset_link())
        expired = stolen.get('/account/reset/%s/set-password/' % self.reset_uid())
        self.assertEqual(expired.status_code, 200)
        self.assertTemplateUsed(expired, 'registration/account_form.html')
        self.assertNotContains(expired, 'new_password1')
        self.assertEqual(stolen.post('/account/reset/%s/set-password/' % self.reset_uid(), {
            'new_password1': 'AttackerPassword26!', 'new_password2': 'AttackerPassword26!'}).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('FreshPassword26!'))
        self.assertFalse(self.user.check_password('AttackerPassword26!'))

    def test_google_reset_eligibility_requires_a_verified_link(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()
        self.client.logout()
        EmailAddress.objects.filter(user=self.user).update(verified=False)
        mail.outbox = []
        self.client.post('/account/reset/', {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 0)

    def test_google_reset_eligibility_requires_the_google_link(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()
        self.client.logout()
        SocialAccount.objects.all().delete()
        mail.outbox = []
        self.client.post('/account/reset/', {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 0)

    def test_passwordless_user_without_google_cannot_reset(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.post('/account/reset/', {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 0)

    def test_unverified_passwordless_user_cannot_reset(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.post('/account/reset/', {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 0)

    def test_oauth_state_and_pkce_and_scope(self):
        params = self.begin(scope='https://www.googleapis.com/auth/drive', auth_params='access_type=offline', next='https://evil.example/')
        self.assertEqual(set(params['scope'][0].split()), {'openid', 'email', 'profile'})
        self.assertEqual(params['access_type'], ['online'])
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertEqual(params['redirect_uri'], ['http://testserver/accounts/google/login/callback/'])
        response = self.finish('incorrect-state')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_state_reuse_is_rejected(self):
        state = self.begin()['state'][0]
        self.assertEqual(self.finish(state).status_code, 302)
        self.assertEqual(self.finish(state).status_code, 403)

    def test_cancel_and_token_exchange_errors_reuse_existing_error_screen(self):
        state = self.begin()['state'][0]
        response = self.client.get('/accounts/google/login/callback/', {'state': state, 'error': 'access_denied'})
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'registration/login.html')
        state = self.begin()['state'][0]
        with patch('allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.get_access_token_data', side_effect=OAuth2Error('test')):
            response = self.client.get('/accounts/google/login/callback/', {'state': state, 'code': 'test'})
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'registration/login.html')

    def test_no_allauth_ui_routes_exposed(self):
        for path in ['/accounts/signup/', '/accounts/login/', '/accounts/email/', '/accounts/social/signup/', '/accounts/google/login/token/']:
            self.assertEqual(self.client.get(path).status_code, 404)

    @override_settings(GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET='')
    def test_missing_credentials_fail_closed_without_affecting_login_page(self):
        self.assertEqual(self.client.get('/accounts/google/login/').status_code, 403)
        self.assertEqual(self.client.get('/login/').status_code, 200)

    def test_the_login_page_offers_google_next_to_the_password_form(self):
        page = self.client.get('/login/')

        self.assertContains(page, 'Sign in with Google')
        self.assertContains(page, 'href="/accounts/google/login/"')
        self.assertContains(page, 'name="password"', count=1)

    def test_a_google_refusal_explains_itself_on_the_login_page(self):
        response = self.authenticate({'email': 'unknown@example.com'})

        self.assertContains(response, 'No Duesdesk account uses that email address. Please sign up first.',
                            status_code=403)
        self.assertContains(response, 'Sign in with Google', status_code=403)

    def test_a_bad_password_still_gets_its_own_explanation(self):
        response = self.client.post('/login/', {'username': self.user.username, 'password': 'wrong'})

        self.assertContains(response, 'The username or password is incorrect')

    def test_other_email_fields_are_not_marked_verified(self):
        self.authenticate()
        self.assertFalse(EmailAddress.objects.filter(email__iexact=self.other.email).exists())
        self.assertEqual(self.member.email, '')

    def test_google_login_rotates_existing_session_key(self):
        session = self.client.session
        session['sample'] = 'pre-login'
        session.save()
        previous = session.session_key
        self.authenticate()
        self.assertNotEqual(previous, self.client.session.session_key)

    def test_pending_invite_does_not_create_account_automatically(self):
        invite = SecretaryInvite.objects.create(organization=self.org, email='invitee@example.com',
            token_hash='a' * 64, created_by=self.user, expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.authenticate({'email': invite.email}).status_code, 403)
        invite.refresh_from_db()
        self.assertIsNone(invite.used_at)
        self.assertFalse(get_user_model().objects.filter(email=invite.email).exists())

    def test_pending_invite_rejection_is_audited_against_the_inviting_organization(self):
        # The invitee is not a user yet, so the attempt is not the invitee's to
        # own. It must not be filed under the inviting organization either.
        SecretaryInvite.objects.create(organization=self.org, email='invitee@example.com',
            token_hash='a' * 64, created_by=self.user, expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.authenticate({'email': 'invitee@example.com'}).status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(action__startswith='google_').exists())
        self.assertEqual(GoogleAuthRejection.objects.get().email, 'invitee@example.com')

    def test_password_login_attempts_do_not_appear_in_the_google_rejection_table(self):
        self.client.post('/login/', {'username': self.user.username, 'password': 'wrong'})
        self.assertFalse(GoogleAuthRejection.objects.exists())

    def test_google_success_ignores_a_next_parameter(self):
        state = self.begin(next='https://evil.example/')['state'][0]
        self.assertRedirects(self.finish(state), '/', fetch_redirect_response=False)

    def test_lockout_does_not_survive_a_google_sign_in(self):
        for _ in range(5):
            self.client.post('/login/', {'username': self.user.username, 'password': 'wrong'})
        self.assertEqual(self.client.post('/login/', {
            'username': self.user.username, 'password': 'ExistingPassword25!'}).status_code, 429)
        # Google proves the account holder's identity independently of the
        # password, so it is not refused and it clears the password lockout.
        self.assertRedirects(self.authenticate(), '/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.client.post('/logout/')
        self.assertEqual(self.client.post('/login/', {
            'username': self.user.username, 'password': 'ExistingPassword25!'}).status_code, 302)

    def test_google_sign_in_is_recorded_by_axes_as_a_login(self):
        self.authenticate()
        logs = AccessLog.objects.filter(username=self.user.username)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.get().path_info, '/accounts/google/login/callback/')

    def test_callback_requires_csrf_state_even_with_valid_claims(self):
        response = self.finish('')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SocialAccount.objects.exists())

    def test_password_post_still_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/login/', {'username': self.user.username, 'password': 'ExistingPassword25!'}).status_code, 403)


class VerifiedEmailHelperTests(TestCase):
    """``AccountAdapter.is_email_verified`` must never widen its own grant."""

    def setUp(self):
        self.org = Organisation.objects.create(name='Organization A')
        self.user = get_user_model().objects.create_user('secretary',
            email='Secretary@example.com', password='ExistingPassword25!')
        self.other = get_user_model().objects.create_user('other', email='other@example.com',
            password='OtherPassword25!')
        UserAccess.objects.create(user=self.user, organization=self.org, role='secretary')
        UserAccess.objects.create(user=self.other, organization=self.org, role='auditor')
        self.factory = RequestFactory()
        self.adapter = get_account_adapter()
        self.member = Member.objects.create(organization=self.org, full_name='Member A',
            joined=date(2026, 1, 1), email='relative@example.com')

    def request(self, user=None, verified=None):
        request = self.factory.get('/')
        request.user = self.user if user is None else user
        request.session = {}
        if verified:
            request.session['google_verified_email'] = verified
        return request

    def link(self):
        EmailAddress.objects.get_or_create(user=self.user, email=self.user.email.lower(),
            defaults={'verified': True, 'primary': True})
        request = self.request()
        request.session['google_verified_email'] = self.user.email.lower()
        return request

    def test_signed_in_google_user_address_is_verified(self):
        self.assertTrue(self.adapter.is_email_verified(self.link(), 'Secretary@example.com'))
        self.assertTrue(self.adapter.is_email_verified(self.link(), 'secretary@EXAMPLE.com'))

    def test_anonymous_visitor_is_never_verified(self):
        from django.contrib.auth.models import AnonymousUser
        request = self.request(user=AnonymousUser(), verified=self.user.email.lower())
        self.assertFalse(self.adapter.is_email_verified(request, self.user.email))

    def test_password_only_session_is_not_verified(self):
        self.assertFalse(self.adapter.is_email_verified(self.request(), self.user.email))

    def test_google_session_without_a_stored_verified_address_is_not_verified(self):
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        request = self.request(verified='someone.else@example.com')
        self.assertFalse(self.adapter.is_email_verified(request, self.user.email))

    def test_stored_address_without_a_verified_email_row_is_not_verified(self):
        request = self.request(verified=self.user.email.lower())
        self.assertFalse(self.adapter.is_email_verified(request, self.user.email))

    def test_another_users_address_is_not_verified(self):
        self.link()
        request = self.request(verified='other@example.com')
        self.assertFalse(self.adapter.is_email_verified(request, 'other@example.com'))
        self.assertFalse(self.adapter.is_email_verified(request, self.other.email))

    def test_unverified_email_row_is_not_verified(self):
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=False, primary=True)
        request = self.request(verified=self.user.email.lower())
        self.assertFalse(self.adapter.is_email_verified(request, self.user.email))

    def test_member_and_contact_addresses_are_never_verified(self):
        self.link()
        for address in (self.member.email, '', 'relative@example.com', 'SECRETARY@example.com '):
            self.assertFalse(self.adapter.is_email_verified(self.link(), address), address)

    def test_signup_is_always_closed(self):
        self.assertFalse(self.adapter.is_open_for_signup(self.request()))


@override_settings(**GOOGLE_SETTINGS)
class SignupVerificationFlowTests(GoogleTestMixin, TestCase):
    """Verifying an address for signup proves nothing beyond the address.

    The separation that matters here: this flow must never sign anybody in, and
    it must never create a user, an organization or a membership. Only the
    ordinary signup submission does that, and only with a current proof in hand.
    """

    def setUp(self):
        self.org = Organisation.objects.create(name='Organization A')
        self.user = get_user_model().objects.create_user('secretary', email='secretary@example.com',
                                                         password='ExistingPassword25!')
        UserAccess.objects.create(user=self.user, organization=self.org, role='secretary')
        self.org_count = Organisation.objects.count()
        self.signup = {'organization_name': 'New Organization', 'username': 'new-signup',
                       'email': 'new-signup@example.com', 'password1': 'A-Very-Strong-Signup-Password!',
                       'password2': 'A-Very-Strong-Signup-Password!'}

    def test_verification_creates_nothing_and_signs_nobody_in(self):
        self.assertEqual(self.verify_signup_email('new-signup@example.com').status_code, 302)

        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(Organisation.objects.count(), self.org_count)
        self.assertFalse(get_user_model().objects.filter(email__iexact='new-signup@example.com').exists())
        self.assertFalse(UserAccess.objects.filter(role='secretary').count() > 1)
        self.assertFalse(SocialAccount.objects.exists())

    def test_verification_returns_to_signup_rather_than_the_app(self):
        response = self.verify_signup_email('new-signup@example.com')

        self.assertEqual(response['Location'], '/signup/')
        # Back on the signup page, still anonymous: verification alone opens nothing.
        self.assertRedirects(self.client.get('/'), '/login/?next=%2F', fetch_redirect_response=False)

    def test_verification_stores_only_the_email_and_the_identity(self):
        self.verify_signup_email('new-signup@example.com', uid='google-uid-1')
        session = self.client.session

        self.assertEqual(session.get('google_flow'), None)  # the flow is finished
        self.assertEqual(session['signup_google_uid'], 'google-uid-1')
        self.assertEqual(session['verified_signup_email']['email'], 'new-signup@example.com')
        self.assertNotIn('access_token', session.get('verified_signup_email', {}))
        self.assertNotIn('google_login', session)

    def test_a_proof_only_covers_the_address_that_was_typed(self):
        # Google proves whatever address the person actually owns. Proving it
        # says nothing about a second address typed into the form afterwards.
        self.verify_signup_email('new-signup@example.com')

        response = self.client.post('/signup/', {**self.signup, 'email': 'other@example.com'})

        self.assertContains(response, 'Verify this email address with Google')
        self.assertFalse(get_user_model().objects.filter(username='new-signup').exists())

    def test_proof_matches_case_insensitively(self):
        self.verify_signup_email('New-Signup@example.com')

        response = self.client.post('/signup/', {**self.signup, 'email': 'NEW-SIGNUP@Example.com'})

        self.assertEqual(response.status_code, 302, response.content[:300])

    def test_verification_of_an_existing_account_is_refused(self):
        # Verifying is not a way in. An address that already has an account can
        # be verified, but only the sign-in flow may open that account.
        response = self.verify_signup_email('secretary@example.com')

        # The signup route refuses and hands the reason back to the signup page.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/signup/')
        self.assertNotIn('verified_signup_email', self.client.session)
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'account already exists')
        self.assertEqual(GoogleAuthRejection.objects.get().flow, 'signup')

    def test_verification_with_an_already_linked_identity_is_refused(self):
        SocialAccount.objects.create(provider='google', uid='google-uid-1', user=self.user,
                                     extra_data={'email': 'secretary@example.com'})

        response = self.verify_signup_email('new-signup@example.com', uid='google-uid-1')

        self.assertEqual(response.status_code, 302)
        self.assertNotIn('verified_signup_email', self.client.session)
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'identity already linked')
        self.assertEqual(SocialAccount.objects.count(), 1)

    def test_unverified_claim_is_refused_in_the_signup_flow(self):
        entry = self.client.post('/accounts/google/signup-verify/', {'email': 'new-signup@example.com'})
        state = parse_qs(urlparse(entry.url).query)['state'][0]

        response = self.google_callback(state, self.google_claims('new-signup@example.com',
                                                                  email_verified=False),
                                        callback='/accounts/google/signup-verify/callback/')

        self.assertEqual(response.status_code, 302)
        self.assertNotIn('verified_signup_email', self.client.session)
        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.reason, 'unverified email')
        self.assertEqual(rejection.flow, 'signup')

    def test_signup_entry_needs_an_email_to_verify(self):
        response = self.client.post('/accounts/google/signup-verify/', {'email': ''})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/signup/')
        self.assertEqual(self.client.session['google_signup_error'],
                         'Enter your email address, then verify it with Google.')

    def test_a_refusal_is_shown_on_the_signup_page_and_creates_nothing(self):
        self.verify_signup_email('secretary@example.com')

        page = self.client.get('/signup/')

        self.assertContains(page, 'An account already exists for this email')
        self.assertContains(page, 'href="/login/"')
        self.assertFalse(get_user_model().objects.filter(username='new-signup').exists())

    def test_the_signin_callback_cannot_verify_for_signup(self):
        state = self.begin()

        response = self.finish(state, {'email': 'new-signup@example.com'})

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('verified_signup_email', self.client.session)
        self.assertFalse(get_user_model().objects.filter(email__iexact='new-signup@example.com').exists())

    def test_the_signup_callback_cannot_sign_anybody_in(self):
        # An existing account must not be opened by aiming its callback at the
        # signup route, even with a matching, verified claim.
        entry = self.client.post('/accounts/google/signup-verify/',
                                 {'email': 'secretary@example.com'})
        state = parse_qs(urlparse(entry.url).query)['state'][0]

        response = self.google_callback(state, self.google_claims('secretary@example.com'),
                                        callback='/accounts/google/login/callback/')

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_starting_signup_discards_a_login_in_progress(self):
        self.begin()
        self.assertIn('google_login', self.client.session)

        self.client.post('/accounts/google/signup-verify/', {'email': 'new-signup@example.com'})

        session = self.client.session
        self.assertEqual(session['google_flow'], 'signup')
        self.assertNotIn('google_login', session)

    def test_starting_login_discards_an_unfinished_signup_proof(self):
        self.verify_signup_email('new-signup@example.com')
        self.assertIn('verified_signup_email', self.client.session)

        self.begin()

        self.assertNotIn('verified_signup_email', self.client.session)
        self.assertNotIn('signup_google_uid', self.client.session)

    def test_a_callback_with_no_flow_is_refused(self):
        state = self.begin()
        session = self.client.session
        session['google_flow'] = 'signup'
        session.save()
        # A login callback arriving with the flow tampered with must not act.
        response = self.finish(state, {'email': 'secretary@example.com'})
        self.assertEqual(response.status_code, 403)

    def test_the_address_verified_at_signup_can_sign_in_later(self):
        self.assertEqual(self.signup_with_google(self.signup).status_code, 302)
        user = get_user_model().objects.get(username='new-signup')
        self.client.logout()

        state = self.begin()
        response = self.finish(state, {'email': 'New-Signup@example.com', 'sub': 'google-signup-1'})

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))
        self.assertEqual(AuditEvent.objects.filter(action='google_login_success').count(), 1)
        self.assertEqual(SocialAccount.objects.filter(user=user, provider='google').count(), 1)

    def test_the_identity_linked_at_signup_is_reused_rather_than_duplicated(self):
        self.assertEqual(self.signup_with_google(self.signup, uid='google-signup-1').status_code, 302)
        user = get_user_model().objects.get(username='new-signup')
        self.client.logout()

        self.finish(self.begin(), {'email': 'new-signup@example.com', 'sub': 'google-signup-1'})

        self.assertEqual(SocialAccount.objects.filter(user=user, provider='google').count(), 1)
        self.assertEqual([e.action for e in AuditEvent.objects.filter(action='google_account_linked')],
                         [])

    def test_signup_never_stores_a_token(self):
        self.assertEqual(self.signup_with_google(self.signup).status_code, 302)

        identity = SocialAccount.objects.get(provider='google')
        self.assertEqual(identity.extra_data, {'email': 'new-signup@example.com', 'email_verified': True})
        self.assertFalse(SocialToken.objects.exists())

    def test_the_signup_flow_records_its_own_audit_action(self):
        self.assertEqual(self.signup_with_google(self.signup).status_code, 302)
        user = get_user_model().objects.get(username='new-signup')

        event = AuditEvent.objects.get(action='signup_email_verified_google')
        self.assertEqual(event.organization_id, user.access.organization_id)
        self.assertEqual(event.actor, user)
        details = json.loads(event.details)
        self.assertEqual(details['flow'], 'signup')
        self.assertEqual(details['ip'], '127.0.0.1')
        self.assertEqual(GoogleAuthRejection.objects.count(), 0)

    def test_a_tampered_proof_timestamp_cannot_unexpire_itself(self):
        self.verify_signup_email('new-signup@example.com')
        session = self.client.session
        session['verified_signup_email'] = {'email': 'new-signup@example.com', 'at': 'not a timestamp'}
        session.save()

        response = self.client.post('/signup/', self.signup)

        self.assertContains(response, 'has expired')
        self.assertFalse(get_user_model().objects.filter(username='new-signup').exists())

    def test_a_proof_for_someone_else_cannot_be_replayed(self):
        self.verify_signup_email('new-signup@example.com')
        session = self.client.session
        session['verified_signup_email'] = 'new-signup@example.com'
        session.save()

        response = self.client.post('/signup/', self.signup)

        self.assertContains(response, 'Verify this email address with Google')
        self.assertFalse(get_user_model().objects.filter(username='new-signup').exists())

    def test_the_signup_button_does_not_submit_the_signup_form(self):
        # The Google control lives inside the signup form but has to leave it.
        # signup.js grabs the first submit button, so this must be an input, and
        # it must carry its own action rather than the form's.
        html = self.client.get('/signup/').content.decode()

        self.assertIn('formaction="/accounts/google/signup-verify/"', html)
        self.assertNotIn('<button class="secondary" formaction', html)

    def test_a_verified_signup_page_hides_the_button_and_shows_the_address(self):
        self.verify_signup_email('new-signup@example.com')
        html = self.client.get('/signup/').content.decode()

        self.assertIn('Verified', html)
        self.assertNotIn('Verify email with Google', html)
        self.assertIn('new-signup@example.com', html)
