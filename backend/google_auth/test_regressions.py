"""Regression coverage for the gaps found during the pending-signup review.

Every test here drives the real requests. The recurring theme is that a stale
Python object must never be more powerful than the current database row: the
service re-reads and re-locks the pending signup on every call, so a caller that
loaded it a minute ago cannot skip the expiry, the attempt limit or the cap.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError
from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone

from ledger.models import AuditEvent, Member, Organisation, UserAccess
from ledger.test_signup import signup_data
from . import flows, signup
from .models import AuthRejection, PendingSignup
from .testsupport import CODE_SETTINGS, VERIFY_PAGE, SignupTestMixin


@override_settings(**CODE_SETTINGS)
class VerificationRegressionTests(SignupTestMixin, TestCase):
    def request(self):
        request = RequestFactory().post(VERIFY_PAGE)
        request.session = self.client.session
        return request

    def test_no_completion_without_email_proof(self):
        self.start_signup(signup_data())
        with self.assertRaises(signup.Invalid):
            signup.complete(self.request(), self.pending())

    def test_expiry_rechecked_even_with_previously_loaded_pending(self):
        self.start_signup(signup_data())
        pending = self.pending()
        PendingSignup.objects.filter(pk=pending.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(signup.check_code(self.request(), pending, self.emailed_code()), signup.MESSAGES['no_pending'])
        self.assertEqual(signup.send_code(self.request(), pending), signup.MESSAGES['no_pending'])
        with self.assertRaises(signup.Invalid):
            signup.complete(self.request(), pending)

    def test_stale_objects_cannot_bypass_attempt_limit(self):
        self.start_signup(signup_data())
        pending = self.pending()
        for _ in range(5):
            signup.check_code(self.request(), pending, 'bad')
        self.assertEqual(signup.check_code(self.request(), pending, self.emailed_code()), signup.MESSAGES['too_many_attempts'])
        self.assertEqual(self.pending().code_attempts, 5)

    def test_stale_objects_cannot_send_a_second_code(self):
        self.start_signup(signup_data())
        pending = self.pending()
        PendingSignup.objects.filter(pk=pending.pk).update(code_sends=[])
        self.assertEqual(signup.send_code(self.request(), pending), '')
        self.assertEqual(signup.send_code(self.request(), pending), signup.MESSAGES['cooldown'])
        self.assertEqual(len(mail.outbox), 2)

    def test_email_failure_is_visible_and_does_not_spend_quota(self):
        with patch('google_auth.signup.send_mail', side_effect=OSError('transport down')):
            self.start_signup(signup_data())
        page = self.client.get(VERIFY_PAGE)
        self.assertContains(page, 'We could not send your code')
        self.assertNotContains(page, 'We emailed you a 6-digit code')
        self.assertEqual(self.pending().code_sends, [])
        self.assertEqual(self.pending().code_hash, '')
        rejection = AuthRejection.objects.get()
        self.assertEqual((rejection.flow, rejection.method), ('signup', 'code'))

    def test_zero_messages_delivered_is_failure(self):
        with patch('google_auth.signup.send_mail', return_value=0):
            self.start_signup(signup_data())
        self.assertFalse(self.pending().code_hash)
        self.assertEqual(AuthRejection.objects.get().reason, 'delivery failed')

    def test_console_backend_never_prints_code(self):
        self.start_signup(signup_data())
        pending = self.pending()
        PendingSignup.objects.filter(pk=pending.pk).update(code_sends=[])
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend'), patch('google_auth.signup.send_mail') as send:
            self.assertEqual(signup.send_code(self.request(), pending), signup.MESSAGES['delivery_failed'])
        send.assert_not_called()

    def test_completion_failure_rolls_back_every_account_record(self):
        self.start_signup(signup_data())
        org_count = Organisation.objects.count()
        with patch('google_auth.signup.UserAccess.objects.create', side_effect=IntegrityError('test failure')):
            page = self.client.post(VERIFY_PAGE, {'action': 'code', 'code': self.emailed_code()})
        self.assertContains(page, 'We could not finish your sign-up')
        self.assertEqual(Organisation.objects.count(), org_count)
        self.assertFalse(User.objects.exists())
        self.assertFalse(UserAccess.objects.exists())
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_existing_email_rechecked_at_completion(self):
        self.start_signup(signup_data())
        User.objects.create_user('competitor', email='NEW-SIGNUP@example.com')
        page = self.client.post(VERIFY_PAGE, {'action': 'code', 'code': self.emailed_code()})
        self.assertContains(page, signup.ALREADY_EXISTS)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(UserAccess.objects.exists())

    def test_replacement_invalidates_previous_session(self):
        self.start_signup(signup_data())
        old_token = self.client.session[flows.PENDING_TOKEN]
        self.client.post('/signup/', signup_data(username='replacement'))
        self.assertNotEqual(old_token, self.pending().token)
        session = self.client.session
        session[flows.PENDING_TOKEN] = old_token
        session.save()
        self.assertEqual(self.client.get(VERIFY_PAGE).status_code, 410)

    def test_a_verified_signup_is_audited_as_verified(self):
        self.signup_with_code(signup_data())
        event = AuditEvent.objects.get(action='signup.email_verified')
        self.assertEqual(json.loads(event.details)['method'], 'code')
        self.assertIsNotNone(event.organization_id)

    def test_code_errors_have_specific_reasons_without_false_expiry(self):
        self.start_signup(signup_data())
        page = self.client.post(VERIFY_PAGE, {'action': 'code', 'code': 'bad'})
        self.assertContains(page, 'That code is not correct')
        self.assertNotContains(page, 'This sign-up has expired')
        row = AuthRejection.objects.get()
        self.assertEqual((row.reason, row.flow, row.method), ('wrong_code', 'signup', 'code'))

    def test_cooldown_page_has_live_countdown(self):
        self.start_signup(signup_data())
        page = self.client.get(VERIFY_PAGE)
        self.assertContains(page, 'verify-email.js')
        self.assertContains(page, 'resend-countdown')
        self.assertGreater(self.pending().resend_wait(), 0)

    def test_password_login_preserves_tenant_scope(self):
        """Signing in grants exactly one organization, and no other."""
        self.signup_with_code(signup_data())
        first = User.objects.get(username='new-signup')
        other_org = Organisation.objects.create(name='Other')
        other = Member.objects.create(organization=other_org, full_name='Private member', joined=timezone.localdate())
        self.client.logout()

        response = self.client.post('/login/', {'username': first.username,
                                                 'password': 'A-Very-Strong-Signup-Password!'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'], str(first.pk))
        self.assertEqual(self.client.get(f'/api/members/{other.pk}/').status_code, 404)
        self.assertNotContains(self.client.get('/api/members/'), 'Private member')