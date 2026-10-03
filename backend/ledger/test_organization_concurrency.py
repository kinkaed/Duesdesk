"""Races around account creation.

Uniqueness under concurrency is the database's job, not the form's: the form's
check is check-then-act, so two requests can both pass it. These tests use real
threads against a database that supports row locks and concurrent transactions;
they are skipped where the backend cannot offer that.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from google_auth.models import EmailClaim

from .models import Organisation, SecretaryInvite, UserAccess
from .organization_views import token_hash
from .test_organizations import STORAGES


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES=STORAGES)
class OrganizationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Concurrent org')
        self.users = [User.objects.create_user('secretary-' + str(i)) for i in range(2)]
        for user in self.users:
            UserAccess.objects.create(user=user, organization=self.org, role='secretary')

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_leaves_keep_one_secretary(self):
        gate = Barrier(2)

        def leave(pk):
            close_old_connections()
            try:
                client = Client()
                client.force_login(User.objects.get(pk=pk))
                gate.wait(timeout=10)
                return client.post('/api/organization/leave/', {}, content_type='application/json').status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(leave, [u.pk for u in self.users]))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(UserAccess.objects.filter(organization=self.org, active=True, role='secretary').count(), 1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_invite_redemption_creates_only_one_account(self):
        token = 'test-concurrent-token'
        SecretaryInvite.objects.create(organization=self.org, token_hash=token_hash(token),
                                       email='invite@example.com', created_by=self.users[0],
                                       expires_at=timezone.now() + timedelta(days=1))
        gate = Barrier(2)

        def join(index):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return Client().post('/signup/?invite=' + token, {
                    'username': 'joined-' + str(index), 'email': 'invite@example.com',
                    'password1': 'Long-Secret-Password-928!',
                    'password2': 'Long-Secret-Password-928!'}).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(join, [1, 2]))

        # One request wins and creates the account; the other finds the invitation
        # already consumed and is refused ordinarily. Neither may answer with a 500.
        self.assertLess(max(results), 500)
        self.assertIn(302, results)
        self.assertEqual(User.objects.filter(email='invite@example.com').count(), 1)
        self.assertEqual(EmailClaim.objects.filter(email='invite@example.com').count(), 1)
        self.assertIsNotNone(SecretaryInvite.objects.get(token_hash=token_hash(token)).used_at)

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_signups_for_one_address_create_only_one_account(self):
        gate = Barrier(2)

        def signup(index):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return Client().post('/signup/', {
                    'organization_name': 'Race Co', 'username': 'race-' + str(index),
                    'email': 'race@example.com',
                    'password1': 'Long-Secret-Password-928!',
                    'password2': 'Long-Secret-Password-928!'}).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(signup, [1, 2]))

        # Exactly one account for the address. The loser's whole transaction --
        # user, organization and membership -- is rolled back by the unique claim.
        self.assertLess(max(results), 500)
        self.assertIn(302, results)
        self.assertEqual(User.objects.filter(email='race@example.com').count(), 1)
        self.assertEqual(EmailClaim.objects.filter(email='race@example.com').count(), 1)
        self.assertEqual(Organisation.objects.filter(name='Race Co').count(), 1)
        self.assertEqual(UserAccess.objects.filter(organization__name='Race Co').count(), 1)
