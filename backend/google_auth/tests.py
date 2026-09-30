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

