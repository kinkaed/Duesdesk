"""Google: one callback, two jobs, and neither of them may guess.

The interesting property of this design is that a single redirect URI serves both
"verify this signup" and "sign in". These tests exist mostly to prove that
sharing it did not blur the two: no flow in the request can pick one, the login
callback can never complete a signup, and the verification callback can never
sign anybody in or create an account that was not already pending.
"""
import json

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings

from ledger.audit_taxonomy import describe, reason_label
from ledger.models import AuditEvent, Organisation, UserAccess
from ledger.test_signup import signup_data

from .models import GoogleAuthRejection, PendingSignup
from .testsupport import (CALLBACK, ENTRY, GOOGLE_SETTINGS, PROVIDERS, VERIFY_PAGE,
                          GoogleTestMixin, mocked_google)

# Storage and hashing stay local so nothing reaches a real provider and no real
# password hasher is used.
LOCAL = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
         'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
FASTER = override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES=LOCAL)


def only_the_default_org():
    """Count organizations that are not the historical default from migration."""
    return Organisation.objects.exclude(name='Membership Association').count()


@FASTER
class GoogleSignupVerificationTests(GoogleTestMixin, TestCase):
    """Verifying a pending signup's address with Google."""

    def data(self, email='new-signup@example.com'):
        return signup_data(email)

    def test_a_matching_google_address_completes_the_signup(self):
        response = self.signup_with_google(self.data())

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        user = User.objects.get(username='new-signup')
        self.assertEqual(user.access.role, 'secretary')
        self.assertEqual(EmailAddress.objects.get(user=user).email, 'new-signup@example.com')
        self.assertFalse(PendingSignup.objects.exists())

    def test_matching_ignores_case(self):
        self.start_signup(self.data())
        response = self.verify_with_google(email='New-Signup@Example.com')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username='new-signup').exists())

    def test_verification_links_the_identity_that_proved_the_address(self):
        self.signup_with_google(self.data(), uid='google-new-uid')

        user = User.objects.get(username='new-signup')
        social = SocialAccount.objects.get(user=user, provider='google')
        self.assertEqual(social.uid, 'google-new-uid')
        self.assertNotIn('access_token', social.extra_data)
        self.assertNotIn('refresh_token', social.extra_data)

    def test_the_verification_is_audited_with_its_method(self):
        self.signup_with_google(self.data())

        user = User.objects.get(username='new-signup')
        events = [e for e in AuditEvent.objects.filter(actor=user)
                  if e.action == 'signup.email_verified']
        self.assertEqual(len(events), 1)
        details = json.loads(events[0].details)
        self.assertEqual(details['method'], 'google')
        # The address is a real column rather than a field in the details JSON, so
        # it can be correlated in a query instead of by parsing every row.
        self.assertEqual(events[0].ip_address, '192.0.2.12')
        self.assertEqual(events[0].organization_id, user.access.organization_id)
        self.assertTrue(events[0].request_id, 'the event must be correlatable with the log')
        # The verification completes inside the provider callback, so the path is
        # the callback the browser actually hit, not the page the user started on.
        self.assertEqual(events[0].path, CALLBACK)

    def test_the_verification_audit_event_reads_as_a_sentence(self):
        # The history is read by people, so the identifier written at signup time
        # has to resolve to a label, a category and a severity without the React
        # side knowing the string exists.
        self.signup_with_google(self.data())

        event = AuditEvent.objects.get(action='signup.email_verified')
        described = describe(event.action, event.outcome, 'new-signup')
        self.assertEqual(described['label'], 'Signed up after verifying their email address')
        self.assertEqual(described['category'], 'accounts')
        self.assertEqual(described['severity'], 'notice')

    def test_a_mismatched_address_creates_nothing_and_offers_the_code(self):
        self.start_signup(self.data())

        self.verify_with_google(email='someone-else@example.com')
        page = self.client.get(VERIFY_PAGE)

        self.assertContains(page,
                            'The Google account email does not match the email you signed up with')
        self.assertContains(page, 'name="code"')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(only_the_default_org(), 0)
        self.assertEqual(UserAccess.objects.count(), 0)

    def test_a_mismatch_is_refused_and_audited_without_an_organization(self):
        self.start_signup(self.data())

        self.verify_with_google(email='someone-else@example.com')

        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.flow, 'signup')
        self.assertEqual(rejection.method, 'google')
        self.assertEqual(rejection.email, 'someone-else@example.com')
        self.assertIsNone(rejection.user)
        # The pending signup is untouched, so the secretary can still finish it.
        self.assertEqual(PendingSignup.objects.count(), 1)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_an_unverified_google_claim_is_refused(self):
        self.start_signup(self.data())

        self.verify_with_google(email_verified=False)

        page = self.client.get(VERIFY_PAGE)
        self.assertContains(page,
                            'The Google account email does not match the email you signed up with')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'unverified email')

    def test_the_code_still_works_after_a_google_mismatch(self):
        self.start_signup(self.data())
        code = self.emailed_code()
        self.verify_with_google(email='someone-else@example.com')

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post(VERIFY_PAGE, {'action': 'code', 'code': code})

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username='new-signup').exists())

    def test_an_identity_already_linked_elsewhere_is_refused(self):
        other = User.objects.create_user('other', email='other@example.com', password='x')
        UserAccess.objects.create(user=other, organization=Organisation.objects.get(),
                                  role='secretary')
        SocialAccount.objects.create(provider='google', uid='google-signup-1', user=other,
                                     extra_data={'email': 'new-signup@example.com'})
        self.start_signup(self.data())

        self.verify_with_google(uid='google-signup-1')

        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'identity already linked')

    def test_the_callback_refuses_when_no_flow_was_started(self):
        self.start_signup(self.data())
        session = self.client.session
        session.pop('google_flow')
        session.save()

        response = self.google_callback('irrelevant', self.google_claims('new-signup@example.com'))

        self.assertEqual(response.url, '/login/')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(GoogleAuthRejection.objects.get().action, 'google.callback_without_flow')

    def test_the_login_entry_point_cannot_complete_a_pending_signup(self):
        self.start_signup(self.data())

        # A session that only ever pressed "Sign in with Google", naming an
        # address that is sitting in somebody else's pending signup.
        stranger = Client()
        response = self.sign_in_with_google('new-signup@example.com', client=stranger)

        self.assertEqual(response.url, '/login/')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(PendingSignup.objects.count(), 1)
        self.assertEqual(EmailAddress.objects.count(), 0)
        self.assertNotIn('_auth_user_id', stranger.session)
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'no matching account')

    def test_completion_rolls_back_when_the_username_is_taken(self):
        self.start_signup(self.data())
        # Somebody takes the name in between, the way a concurrent signup would.
        User.objects.create_user('new-signup', email='someone@example.com', password='x')

        self.verify_with_google()
        page = self.client.get(VERIFY_PAGE)

        self.assertContains(page, 'That username is already taken')
        # Only the squatter survives. No organization, no access row, no code
        # spent, no identity linked.
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(only_the_default_org(), 0)
        self.assertEqual(UserAccess.objects.count(), 0)
        self.assertEqual(PendingSignup.objects.count(), 1)
        self.assertEqual(PendingSignup.objects.get().code_hash, self.pending_code_hash)
        self.assertIsNone(self.client.session.get('_auth_user_id'))


@FASTER
class GoogleLoginTests(GoogleTestMixin, TestCase):
    password = 'A-Very-Strong-Login-Password!'

    def setUp(self):
        self.signup_with_code(signup_data('founder@example.com', username='founder',
                                           organization='Founder Co',
                                           password=self.password))
        self.user = User.objects.get(username='founder')
        self.client.logout()

    def test_login_page_has_one_google_button_and_an_unchanged_form(self):
        page = self.client.get('/login/')

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.content.count(b'Sign in with Google'), 1)
        self.assertContains(page, 'name="username"')
        self.assertContains(page, 'name="password"')
        self.assertContains(page, 'Forgot your password?')

    def test_an_existing_user_can_sign_in_with_google(self):
        response = self.sign_in_with_google('founder@example.com')

        self.assertEqual(response.url, '/')
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.assertTrue(SocialAccount.objects.filter(user=self.user, provider='google').exists())

    def test_signing_in_links_the_identity_without_replacing_a_password(self):
        self.sign_in_with_google('founder@example.com')

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))
        self.assertTrue(EmailAddress.objects.get(user=self.user).verified)

    def test_matching_ignores_case(self):
        response = self.sign_in_with_google('Founder@Example.com')

        self.assertEqual(response.url, '/')
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))

    def test_a_linked_identity_is_audited_as_a_known_user(self):
        self.sign_in_with_google('founder@example.com')

        events = [e for e in AuditEvent.objects.filter(actor=self.user)
                  if e.action in ('google.login_success', 'google.account_linked')]
        self.assertEqual(sorted(e.action for e in events),
                         ['google.account_linked', 'google.login_success'])
        self.assertTrue(all(e.organization_id == self.user.access.organization_id
                            for e in events))
        self.assertEqual([json.loads(e.details)['method'] for e in events], ['google', 'google'])
        # Both are now ordinary tenant events: correlated, addressed, and
        # described in words by the shared taxonomy rather than by a screen
        # special-casing these two strings.
        self.assertTrue(all(e.request_id and e.path and e.ip_address for e in events))
        self.assertEqual(describe('google.login_success')['label'], 'Signed in with Google')
        self.assertEqual(describe('google.account_linked')['category'], 'security')
        self.assertFalse(GoogleAuthRejection.objects.exists())

    def test_an_unknown_address_is_refused_with_an_invitation_to_sign_up(self):
        response = self.sign_in_with_google('stranger@example.com')

        self.assertEqual(response.url, '/login/')
        page = self.client.get('/login/')
        self.assertContains(page, 'No account found for this Google email. Please sign up first.')
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertEqual(PendingSignup.objects.count(), 0)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signing_in_never_creates_anything(self):
        for address in ('stranger@example.com', 'STRANGER@example.com', 'nobody@example.com'):
            self.client.logout()
            self.sign_in_with_google(address)

        self.assertEqual(PendingSignup.objects.count(), 0)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(only_the_default_org(), 1)

    def test_an_unverified_claim_is_refused(self):
        response = self.sign_in_with_google('founder@example.com', email_verified=False)

        self.assertEqual(response.url, '/login/')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'unverified email')

    def test_a_missing_email_claim_is_refused(self):
        response = self.sign_in_with_google('founder@example.com', email=None)

        self.assertEqual(response.url, '/login/')
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'no email claim')

    def test_an_inactive_account_is_refused(self):
        self.user.is_active = False
        self.user.save()

        response = self.sign_in_with_google('founder@example.com')

        self.assertEqual(response.url, '/login/')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'inactive account')

    def test_a_refusal_against_a_known_account_reaches_the_tenant_audit(self):
        # The rejection table alone is not enough: nothing in the application
        # reads it, so a member being refused a sign-in would leave no trace in
        # the history their own secretary reviews. The account is known here, so
        # the refusal is filed under its organization as well.
        self.user.is_active = False
        self.user.save()

        self.sign_in_with_google('founder@example.com')

        event = AuditEvent.objects.get(action='google.login_rejected')
        self.assertEqual(event.organization_id, self.user.access.organization_id)
        self.assertEqual(event.actor_id, self.user.pk)
        self.assertEqual(event.outcome, 'rejected')
        self.assertEqual(event.reason, 'inactive account')
        self.assertTrue(event.request_id and event.path)
        # Both records exist: the tenantless row keeps the address and the flow
        # for correlating repeats, the tenant row is the one a reader can see.
        self.assertEqual(GoogleAuthRejection.objects.count(), 1)
        described = describe(event.action, event.outcome, 'founder')
        self.assertEqual(described['label'], 'A Google sign-in was refused')
        # A refusal is promoted above its base severity, the same as every other
        # non-success outcome, so it cannot be filed alongside routine successes.
        self.assertEqual(described['severity'], 'critical')
        self.assertEqual(reason_label(event.reason, event.outcome),
                         'The account has been disabled.')

    def test_a_refusal_against_an_unknown_address_stays_tenantless(self):
        # There is no account and so no organization. Inventing one would put a
        # stranger's failed sign-in in somebody else's history, so the row stays
        # in the rejection table, where no tenant can read it.
        self.sign_in_with_google('stranger@example.com')

        self.assertEqual(GoogleAuthRejection.objects.get().action, 'google.login_rejected')
        self.assertFalse(AuditEvent.objects.filter(action='google.login_rejected').exists())
        # The attempt is still fully recorded, just not as a tenant event.
        rejection = GoogleAuthRejection.objects.get()
        self.assertEqual(rejection.email, 'stranger@example.com')
        self.assertIsNone(rejection.user_id)
        self.assertTrue(rejection.ip)

    def test_a_linked_identity_claiming_another_address_is_refused(self):
        self.sign_in_with_google('founder@example.com')
        social = SocialAccount.objects.get(user=self.user)
        social.extra_data = {'email': 'someone-else@example.com'}
        social.save()
        self.client.logout()

        response = self.sign_in_with_google('founder@example.com')

        self.assertEqual(response.url, '/login/')
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'identity email mismatch')

    def test_a_google_only_account_can_still_reset_its_password(self):
        self.sign_in_with_google('founder@example.com')
        self.user.set_unusable_password()
        self.user.save()
        self.client.logout()

        already_sent = len(mail.outbox)

        with override_settings(**GOOGLE_SETTINGS):
            response = self.client.post('/account/reset/', {'email': 'founder@example.com'})

        self.assertRedirects(response, '/account/reset/sent/', fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), already_sent + 1)

    def test_google_does_not_take_the_account_over_from_a_password(self):
        self.sign_in_with_google('founder@example.com')

        self.user.refresh_from_db()
        self.assertTrue(self.user.has_usable_password())
        self.assertTrue(self.user.check_password(self.password))


@FASTER
class ProviderConfigurationTests(GoogleTestMixin, TestCase):
    """The provider is configured the way the design requires."""

    def test_tokens_are_never_stored_and_the_scopes_are_minimal(self):
        from allauth.socialaccount import app_settings as social_settings

        self.assertFalse(social_settings.STORE_TOKENS)
        self.assertEqual(sorted(social_settings.PROVIDERS['google']['SCOPE']),
                         ['email', 'openid', 'profile'])

    def test_the_callback_is_the_only_redirect_uri(self):
        from django.urls import reverse

        self.assertEqual(reverse('google_callback'), CALLBACK)
        self.assertEqual(reverse('google_login'), ENTRY)
        self.assertEqual(reverse('google_verify'), '/accounts/google/verify/')

    def test_a_deployment_without_credentials_refuses_before_leaving(self):
        # An unconfigured deployment must not send a browser to Google with an
        # empty client id. Both entry points fail closed, with a reason recorded.
        blank = {'google': {**PROVIDERS['google'],
                            'APP': {'client_id': '', 'secret': '', 'key': ''}}}
        self.start_signup(signup_data('new-signup@example.com', username='new-signup',
                                      organization='New Organization'))
        unconfigured = {**GOOGLE_SETTINGS, 'GOOGLE_CLIENT_ID': '',
                        'GOOGLE_CLIENT_SECRET': '', 'SOCIALACCOUNT_PROVIDERS': blank}
        stranger = Client()
        with override_settings(**unconfigured):
            # A separate browser, because starting a sign-in deliberately
            # abandons any signup this session was verifying.
            sign_in = stranger.get(ENTRY)
            verify = self.client.post('/accounts/google/verify/')
        sign_in_page = stranger.get('/login/')
        page = self.client.get(VERIFY_PAGE)

        self.assertEqual(sign_in.url, '/login/')
        self.assertContains(sign_in_page, 'not available on this deployment')
        # The verification page stays exactly where it was, still offering the
        # emailed code, which needs no credentials at all.
        self.assertEqual(verify.url, VERIFY_PAGE)
        self.assertContains(page, '6-digit code')
        self.assertContains(page, 'not available')
        self.assertEqual(
            sorted(GoogleAuthRejection.objects.values_list('flow', 'reason')),
            [('login', 'provider not configured'), ('signup', 'provider not configured')])

    def test_a_claim_with_no_identity_is_refused_before_any_write(self):
        with override_settings(**GOOGLE_SETTINGS):
            state = self.begin()
            token, id_token = mocked_google({'email_verified': True})
            with token, id_token:
                response = self.google_callback(state, {'email_verified': True})

        self.assertEqual(response.url, '/login/')
        # The reply was too malformed for allauth to read at all, so it is
        # refused as a provider failure. Nothing was written either way.
        self.assertEqual(GoogleAuthRejection.objects.get().reason, 'provider error')
        self.assertFalse(SocialAccount.objects.exists())
        self.assertFalse(EmailAddress.objects.exists())
        self.assertFalse(PendingSignup.objects.exists())

    def test_no_allauth_route_reaches_signup_or_confirmation_pages(self):
        for path in ('/accounts/signup/', '/accounts/confirm-email/',
                     '/accounts/social/signup/google/', '/accounts/social/connections/',
                     '/accounts/password/change/'):
            self.assertEqual(self.client.get(path).status_code, 404, path)