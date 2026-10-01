"""Google: one callback, two jobs, and neither of them may guess.

The interesting property of this design is that a single redirect URI serves both
"verify this signup" and "sign in". These tests exist mostly to prove that
sharing it did not blur the two: no flow in the request can pick one, the login
callback can never complete a signup, and the verification callback can never
sign anybody in or create an account that was not already pending.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from ledger.audit_taxonomy import describe, reason_label
from ledger.models import AuditEvent, Organisation, UserAccess
from ledger.test_signup import signup_data

from . import signup
from .models import GoogleAuthRejection, PendingSignup
from .testsupport import (CALLBACK, ENTRY, GOOGLE_SETTINGS, PROVIDERS, SIGNUP_PAGE,
                          VERIFY_PAGE, GoogleTestMixin, mocked_google)

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


@FASTER
class SignupSendCapTests(GoogleTestMixin, TestCase):
    """How many verification codes one address can be sent.

    The cap is enforced against the send history held on the pending signup, so
    these tests exist to prove that nothing a visitor can reach destroys that
    history. Reposting the form used to delete the row and with it the record of
    what had already been sent, which made the cap something a script could step
    straight over.
    """

    address = 'capped@example.com'

    def backdate_sends(self, count, minutes=5, address=None):
        """Age the recorded sends so only the hourly cap can be in the way.

        Old enough to clear the 60-second cooldown, recent enough to still count
        against the sliding one-hour cap. A stamp older than the window would be
        pruned and prove nothing.
        """
        pending = PendingSignup.objects.get(email=address or self.address)
        earlier = (timezone.now() - timedelta(minutes=minutes)).isoformat()
        pending.code_sends = [earlier] * count
        pending.save(update_fields=['code_sends'])
        return pending

    def age_sends(self, minutes=5):
        """Slide every recorded send back, so the history keeps growing naturally."""
        pending = PendingSignup.objects.get(email=self.address)
        earlier = timezone.now() - timedelta(minutes=minutes)
        pending.code_sends = [(earlier + timedelta(seconds=i)).isoformat()
                             for i in range(len(pending.code_sends))]
        pending.save(update_fields=['code_sends'])
        return pending

    def repost(self, client=None, username='capped-user'):
        return self.start_signup(signup_data(self.address, username=username),
                                client=client)

    def test_the_first_signup_still_sends_a_code(self):
        self.repost()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(PendingSignup.objects.filter(email=self.address).count(), 1)

    def test_reposting_cannot_reset_the_cap(self):
        """The regression: a full cap must survive a resubmission."""
        self.repost()
        self.backdate_sends(signup.SENDS_PER_HOUR)  # now 5 sends in the last hour
        mail.outbox.clear()

        self.repost(username='second-attempt')

        # History remains; the sixth send in the same hour is rejected by the cap
        self.assertEqual(mail.outbox, [], 'the hourly cap was not enforced')
        pending = PendingSignup.objects.get(email=self.address)
        self.assertEqual(len(pending.code_sends), signup.SENDS_PER_HOUR)
        self.assertEqual(pending.username, 'second-attempt')

    def test_repeated_reposts_never_exceed_the_hourly_cap(self):
        """Repost far more often than the cap allows; count what actually goes out.

        Each repost is staged as arriving five minutes after the last one, so the
        cooldown is always satisfied and it is the sliding hourly cap that has to
        hold. Two independent limits, neither of which a resubmission can reset.
        """
        for index in range(12):
            self.repost(username=f'attempt-{index}')
            # Stand in for five minutes passing.
            self.age_sends()

        self.assertLessEqual(len(mail.outbox), signup.SENDS_PER_HOUR)
        self.assertEqual(PendingSignup.objects.filter(email=self.address).count(), 1)

    def test_reposting_inside_the_cooldown_sends_nothing(self):
        self.repost()
        mail.outbox.clear()

        self.repost(username='second-attempt')

        self.assertEqual(mail.outbox, [], 'the cooldown did not hold')
        self.assertEqual(len(PendingSignup.objects.get(email=self.address).code_sends), 1)

    def test_a_repost_still_voids_the_previous_code(self):
        """Replacing the row must not leave a usable code behind."""
        self.repost()
        self.start_signup(signup_data(self.address, username='second-attempt'))

        pending = PendingSignup.objects.get(email=self.address)
        self.assertEqual(pending.code_hash, '')
        self.assertIsNone(pending.code_expires_at)
        self.assertEqual(pending.code_attempts, 0)
        self.assertFalse(pending.code_dead)

    def test_the_cap_is_per_address_so_signups_do_not_block_each_other(self):
        self.repost()
        self.backdate_sends(signup.SENDS_PER_HOUR)
        mail.outbox.clear()

        # A second person signing up must not inherit the cap.
        second = Client()
        self.start_signup(signup_data('other@example.com', username='other-user'),
                          client=second)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(PendingSignup.objects.filter(email='other@example.com')), 1)

    def test_capping_a_send_does_not_become_a_probe_for_who_has_an_account(self):
        """A capped address must look like any other, not like a special case."""
        self.repost()
        self.backdate_sends(signup.SENDS_PER_HOUR)

        capped = self.start_signup(signup_data(self.address, username='second-attempt'))
        # The refusal is stashed in the session by the redirect and shown on the
        # verification page it lands on.
        message = self.client.get(capped.url).context['error']

        self.assertEqual(message, signup.MESSAGES['hourly_cap'])
        self.assertNotIn('already', message.lower())

        # An address with no account at all gets the identical treatment once its
        # own budget is spent, so the cap reveals nothing about who is registered.
        other = Client()
        landed = self.start_signup(signup_data('stranger@example.com',
                                               username='stranger-user'), client=other)
        self.backdate_sends(signup.SENDS_PER_HOUR, address='stranger@example.com')
        stranger_message = other.post(VERIFY_PAGE, {'action': 'resend'}).context['error']

        self.assertEqual(stranger_message, message)

    def test_a_registered_address_is_still_told_to_log_in(self):
        """Guards the other side of it: the existing refusal must survive."""
        self.signup_with_code(signup_data('founder@example.com', username='founder'))
        self.client.logout()

        taken = self.client.post('/signup/', signup_data('founder@example.com',
                                                         username='founder2'))
        self.assertEqual(taken.status_code, 200)
        self.assertIn('already exists', taken.context['form'].errors['email'][0])


class SignupDeliveryFailureTests(GoogleTestMixin, TestCase):
    """What happens when the provider accepts the request but sends nothing.

    fail_silently=True made every one of these outcomes indistinguishable from
    success: send_mail() returns the number of messages delivered, and 0 was
    discarded along with the exception. The page then said "We emailed you a
    6-digit code" and asked for a code that was never sent, while the cooldown
    and the hourly cap had already been spent on the attempt that sent nothing.
    """

    address = 'undeliverable@example.com'

    def failing_provider(self, reason='smtp down'):
        # An exception instance, not a bare string: mock hands a non-exception
        # side_effect straight back as the return value, which would have been a
        # third thing to test.
        return patch('google_auth.signup.send_mail', side_effect=OSError(reason))

    def error_shown_after(self, landed):
        """The refusal is stashed in the session by the redirect and shown on the
        verification page it lands on, the same way the cap refusal is."""
        self.assertEqual(landed.status_code, 302, landed.content[:300])
        self.assertEqual(landed.url, VERIFY_PAGE)
        return str(self.client.get(landed.url).context['error'])

    def test_a_provider_error_is_reported_rather_than_reported_as_sent(self):
        with self.failing_provider():
            landed = self.client.post(SIGNUP_PAGE, signup_data(self.address))

        self.assertEqual(self.error_shown_after(landed),
                         signup.MESSAGES['delivery_failed'])

    def test_a_provider_that_returns_zero_is_treated_as_a_failure(self):
        # The other shape of the same fault: no exception, nothing delivered.
        # Only checking for a raised error would have left this one reporting
        # success.
        with patch('google_auth.signup.send_mail', return_value=0):
            landed = self.client.post(SIGNUP_PAGE, signup_data(self.address))

        self.assertEqual(self.error_shown_after(landed),
                         signup.MESSAGES['delivery_failed'])

    def test_a_failed_send_leaves_no_usable_code_behind(self):
        with self.failing_provider():
            self.client.post(SIGNUP_PAGE, signup_data(self.address))

        pending = PendingSignup.objects.get(email=self.address)
        self.assertEqual(pending.code_hash, '')
        self.assertIsNone(pending.code_expires_at)
        self.assertEqual(pending.code_attempts, 0)
        self.assertFalse(pending.code_dead)
        self.assertFalse(pending.code_live(),
                         'a code nobody received must not be one guess can reach')

    def test_a_failed_send_does_not_spend_the_cooldown(self):
        # The user is told to try again, so trying again must work. Leaving the
        # send stamped would refuse their retry with the cooldown message and
        # leave them stuck on a page whose button does nothing.
        with self.failing_provider():
            self.client.post(SIGNUP_PAGE, signup_data(self.address))
        self.assertEqual(PendingSignup.objects.get(email=self.address).code_sends, [])

        with override_settings(**GOOGLE_SETTINGS):
            retry = self.client.post(SIGNUP_PAGE,
                                     signup_data(self.address, username='second-try'))

        self.assertEqual(retry.status_code, 302, retry.content[:300])
        self.assertEqual(retry.url, VERIFY_PAGE)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(PendingSignup.objects.get(email=self.address).code_live())

    def test_a_failed_send_creates_nothing_that_could_be_signed_in_to(self):
        with self.failing_provider():
            self.client.post(SIGNUP_PAGE, signup_data(self.address))

        self.assertEqual(PendingSignup.objects.filter(email=self.address).count(), 1)
        self.assertFalse(User.objects.filter(email=self.address).exists())
        self.assertEqual(only_the_default_org(), 0)

    def test_a_working_provider_is_unaffected(self):
        with override_settings(**GOOGLE_SETTINGS):
            landed = self.client.post(SIGNUP_PAGE, signup_data(self.address))

        self.assertEqual(landed.status_code, 302, landed.content[:300])
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(PendingSignup.objects.get(email=self.address).code_live())
        self.assertEqual(len(PendingSignup.objects.get(email=self.address).code_sends), 1)