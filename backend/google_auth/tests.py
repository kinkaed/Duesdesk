"""Signup creates a usable account immediately.

There is no verification step, so these tests are about what *does* happen on the
one unauthenticated write path: a user, an organization (or an invited join), a
membership and an email claim, all committed together and signed in, or nothing at
all. The rate limit is a throughput control and is tested as one; uniqueness under
concurrency is proved in ledger.test_organization_concurrency.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from ledger.models import AuditEvent, Organisation, SecretaryInvite, UserAccess
from ledger.organization_views import token_hash
from ledger.test_signup import signup_data

from . import create
from .models import EmailClaim
from .testsupport import SignupTestMixin

FASTER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


def new_organizations():
    """Organizations other than the historical default created by migration."""
    return Organisation.objects.exclude(name='Membership Association')


@FASTER
class ImmediateCreationTests(SignupTestMixin, TestCase):
    def test_a_signup_creates_and_signs_in_the_account_at_once(self):
        response = self.start_signup(signup_data())

        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(response.url, '/signup/branding/')
        user = User.objects.get(username='new-signup')
        self.assertTrue(user.check_password('A-Very-Strong-Signup-Password!'))
        self.assertNotEqual(user.password, 'A-Very-Strong-Signup-Password!')
        self.assertEqual(user.email, 'new-signup@example.com')
        self.assertEqual(user.access.role, 'secretary')
        self.assertEqual(user.access.organization.name, 'New Organization')
        self.assertTrue(user.access.organization.onboarding_required)
        self.assertIsNone(user.access.organization.onboarded_at)
        self.assertEqual(EmailClaim.objects.get(user=user).email, 'new-signup@example.com')
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))

    def test_nothing_is_created_before_the_form_is_posted(self):
        self.client.get('/signup/')

        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(new_organizations().count(), 0)
        self.assertEqual(UserAccess.objects.count(), 0)
        self.assertEqual(EmailClaim.objects.count(), 0)

    def test_the_address_is_stored_normalized(self):
        self.start_signup(signup_data(email='  Mixed.Case@Example.com '))

        user = User.objects.get(username='new-signup')
        self.assertEqual(user.email, 'mixed.case@example.com')
        self.assertEqual(EmailClaim.objects.get(user=user).email, 'mixed.case@example.com')

    def test_the_created_organization_is_audited(self):
        self.signup_with_code(signup_data())

        event = AuditEvent.objects.get(action='organization.created')
        self.assertEqual(event.organization, User.objects.get(username='new-signup').access.organization)
        self.assertEqual(event.actor_id, User.objects.get(username='new-signup').pk)


@FASTER
class DuplicateAddressTests(TestCase):
    password = 'A-Very-Strong-Signup-Password!'

    def test_an_existing_account_is_refused_with_the_exact_message(self):
        User.objects.create_user('existing', email='person@example.com', password=self.password)

        response = self.client.post('/signup/', signup_data(email='PERSON@example.com'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'An account with this email already exists, please log in.')
        self.assertFalse(User.objects.filter(username='new-signup').exists())
        self.assertEqual(EmailClaim.objects.count(), 0)

    def test_an_existing_claim_blocks_a_new_account(self):
        user = User.objects.create_user('claimed', email='other@example.com', password=self.password)
        EmailClaim.objects.create(email='claimed@example.com', user=user)

        response = self.client.post('/signup/', signup_data(email='claimed@example.com',
                                                            username='different'))

        self.assertContains(response, 'An account with this email already exists, please log in.')
        self.assertFalse(User.objects.filter(username='different').exists())

    def test_a_taken_username_rolls_everything_back(self):
        User.objects.create_user('new-signup', email='taken@example.com', password=self.password)

        response = self.client.post('/signup/', signup_data())

        self.assertContains(response, 'already exists')
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(new_organizations().count(), 0)
        self.assertEqual(UserAccess.objects.count(), 0)

    def test_mismatched_passwords_create_nothing(self):
        response = self.client.post('/signup/', {**signup_data(), 'password2': 'Different-Password!'})

        self.assertContains(response, 'The two password fields didn')
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(new_organizations().count(), 0)

    def test_a_lost_race_reads_as_the_same_duplicate_refusal(self):
        """A unique claim violation is what the concurrent loser actually gets.

        The form's check passed for both requests, so the database refused the
        second claim. The visitor must see the ordinary duplicate message, and the
        loser's whole transaction -- user and organization included -- must be gone.
        """
        from unittest.mock import patch

        from django.db import IntegrityError

        with patch('google_auth.create.EmailClaim.objects.create',
                   side_effect=IntegrityError('duplicate key')):
            response = self.client.post('/signup/', signup_data(email='race@example.com',
                                                                username='racey'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'An account with this email already exists, please log in.')
        self.assertFalse(User.objects.filter(username='racey').exists())
        self.assertEqual(new_organizations().count(), 0)
        self.assertEqual(EmailClaim.objects.count(), 0)


@FASTER
class InvitationTests(SignupTestMixin, TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Inviting Organization')
        self.inviter = User.objects.create_user('inviter', email='inviter@example.com')
        UserAccess.objects.create(user=self.inviter, organization=self.org, role='secretary')

    def invite(self, email='new-signup@example.com', raw='an-invitation-token', **overrides):
        fields = dict(organization=self.org, email=email, created_by=self.inviter,
                      token_hash=token_hash(raw), expires_at=timezone.now() + timedelta(days=1))
        fields.update(overrides)
        return SecretaryInvite.objects.create(**fields)

    def test_an_invited_signup_joins_the_organization_and_consumes_the_invite(self):
        invite = self.invite()

        response = self.start_signup(signup_data(), invite='an-invitation-token')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/overview/')
        user = User.objects.get(username='new-signup')
        self.assertEqual(user.access.organization, self.org)
        self.assertEqual(user.access.role, 'secretary')
        self.assertFalse(user.access.organization.onboarding_required)
        invite.refresh_from_db()
        self.assertEqual(invite.used_by, user)
        self.assertIsNotNone(invite.used_at)
        self.assertEqual(AuditEvent.objects.get(action='organization.joined').organization, self.org)

    def test_an_invitation_is_bound_to_its_address(self):
        self.invite()

        response = self.start_signup(signup_data(email='wrong@example.com'), invite='an-invitation-token')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Use the email address named in your invitation.')
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_a_used_invitation_is_refused(self):
        self.invite(used_at=timezone.now())

        response = self.client.get('/signup/?invite=an-invitation-token')

        self.assertEqual(response.status_code, 400)

    def test_a_revoked_or_expired_invitation_is_refused(self):
        for overrides in ({'revoked_at': timezone.now()},
                          {'expires_at': timezone.now() - timedelta(seconds=1)}):
            SecretaryInvite.objects.all().delete()
            self.invite(**overrides)
            self.assertEqual(self.client.get('/signup/?invite=an-invitation-token').status_code, 400)

    def test_a_disabled_organization_cannot_be_joined(self):
        self.invite()
        Organisation.objects.filter(pk=self.org.pk).update(disabled_at=timezone.now())

        self.assertEqual(self.client.get('/signup/?invite=an-invitation-token').status_code, 400)
        self.assertFalse(User.objects.filter(username='new-signup').exists())

    def test_an_invitation_cannot_be_replayed(self):
        self.invite()
        self.start_signup(signup_data(), invite='an-invitation-token')
        # The invitation is single use: posting the same token again is refused.
        self.assertEqual(Client().get('/signup/?invite=an-invitation-token').status_code, 400)


@FASTER
class RateLimitTests(SignupTestMixin, TestCase):
    def test_the_ip_bucket_refuses_the_sixth_attempt(self):
        for index in range(create.PER_IP_PER_HOUR):
            client = Client()
            response = self.start_signup(signup_data(email=f'person{index}@example.com',
                                                     username=f'person{index}'), client=client)
            self.assertEqual(response.status_code, 302, response.content)

        blocked = self.start_signup(signup_data(email='late@example.com', username='late'))

        self.assertEqual(blocked.status_code, 429)
        self.assertContains(blocked, 'Too many sign-up attempts', status_code=429)
        self.assertFalse(User.objects.filter(username='late').exists())

    def test_the_email_bucket_is_normalized_like_the_form(self):
        for _ in range(create.PER_EMAIL_PER_HOUR):
            create.record_attempt('Capped@Example.com', '198.51.100.7')

        self.assertTrue(create.rate_limited('capped@example.com', '198.51.100.8'))
        # A different address from a fresh IP is untouched.
        self.assertFalse(create.rate_limited('other@example.com', '198.51.100.9'))

    def test_old_attempts_fall_out_of_the_window(self):
        create.record_attempt('aged@example.com', '198.51.100.7')
        from google_auth.models import SignupAttempt
        SignupAttempt.objects.update(created_at=timezone.now() - timedelta(hours=2))

        self.assertFalse(create.rate_limited('aged@example.com', '198.51.100.7'))


@FASTER
class PasswordScopeTests(SignupTestMixin, TestCase):
    def test_a_created_account_can_log_in_and_only_sees_its_own_organization(self):
        self.start_signup(signup_data())
        self.client.logout()
        other = Organisation.objects.create(name='Other')

        response = self.client.post('/login/', {'username': 'new-signup',
                                                'password': 'A-Very-Strong-Signup-Password!'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'],
                         str(User.objects.get(username='new-signup').pk))
        self.assertNotEqual(User.objects.get(username='new-signup').access.organization_id, other.pk)
