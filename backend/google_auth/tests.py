"""Signup verification: a code is emailed, it proves the address, then the account.

Signup is the only unauthenticated path that creates anything, so these tests are
about what must *not* happen until a correct code has been entered: no user, no
organization, no membership, no session. They also cover the two places the flow
is easy to break -- the send cap that stops one address being emailed endlessly,
and a mail transport that accepts the request and delivers nothing.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from ledger.models import Organisation, UserAccess
from ledger.test_signup import signup_data

from . import signup
from .models import PendingSignup
from .testsupport import (CODE_SETTINGS, SIGNUP_PAGE, VERIFY_PAGE, SignupTestMixin)

# Hashing stays cheap so the suite is not dominated by PBKDF2.
FASTER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


def only_the_default_org():
    """Count organizations that are not the historical default from migration."""
    return Organisation.objects.exclude(name='Membership Association').count()


@FASTER
class SignupCompletionTests(SignupTestMixin, TestCase):
    """Completing a signup, and refusing to complete one too early."""

    def data(self, email='new-signup@example.com'):
        return signup_data(email)

    def test_a_correct_code_creates_the_account_and_signs_in(self):
        self.start_signup(self.data())

        landed = self.enter_code()

        self.assertEqual(landed.status_code, 302)
        self.assertEqual(landed.url, '/')
        self.assertTrue(User.objects.filter(email='new-signup@example.com').exists())
        self.assertEqual(UserAccess.objects.get().role, 'secretary')
        # The pending row is consumed, not left behind as something to retry.
        self.assertEqual(PendingSignup.objects.count(), 0)
        self.assertEqual(self.client.session.get('_auth_user_id'),
                         str(User.objects.get(email='new-signup@example.com').pk))

    def test_nothing_is_created_before_the_code_is_entered(self):
        self.start_signup(self.data())

        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(only_the_default_org(), 0)
        self.assertEqual(UserAccess.objects.count(), 0)
        self.assertIsNone(self.client.session.get('_auth_user_id'))
        # The pending row exists and holds a hash, never the digits.
        pending = self.pending()
        self.assertNotEqual(pending.code_hash, self.emailed_code())

    def test_completion_rolls_back_when_the_username_is_taken(self):
        self.start_signup(self.data())
        # Somebody takes the name in between, the way a concurrent signup would.
        User.objects.create_user('new-signup', email='someone@example.com', password='x')
        code = self.emailed_code()

        page = self.enter_code(code=code)

        self.assertContains(page, 'That username is already taken')
        # Only the squatter survives. No organization, no access row.
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(only_the_default_org(), 0)
        self.assertEqual(UserAccess.objects.count(), 0)
        self.assertEqual(PendingSignup.objects.count(), 1)
        self.assertIsNone(self.client.session.get('_auth_user_id'))

    def test_completion_requires_the_code_to_have_been_proven_first(self):
        """Reaching complete() without a proven code must not create anything.

        The verified_at timestamp is what the code path sets, and complete()
        re-checks it rather than trusting its caller, so this cannot be skipped by
        anything that finds a way to call it directly.
        """
        self.start_signup(self.data())
        pending = self.pending()

        with self.assertRaises(signup.Invalid):
            signup.complete(self.client, pending)

        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(only_the_default_org(), 0)


@FASTER
class SignupSendCapTests(SignupTestMixin, TestCase):
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
        other.post(SIGNUP_PAGE, signup_data('stranger@example.com',
                                            username='stranger-user'))
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


class SignupDeliveryFailureTests(SignupTestMixin, TestCase):
    """What happens when the mail transport accepts the request but sends nothing.

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

        with override_settings(**CODE_SETTINGS):
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
        with override_settings(**CODE_SETTINGS):
            landed = self.client.post(SIGNUP_PAGE, signup_data(self.address))

        self.assertEqual(landed.status_code, 302, landed.content[:300])
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(PendingSignup.objects.get(email=self.address).code_live())
        self.assertEqual(len(PendingSignup.objects.get(email=self.address).code_sends), 1)