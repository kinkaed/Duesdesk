"""Account area: the profile route, the current-user endpoint, logout and password change.

These cover the account surface the profile page is built on. The point of most of
these tests is that the profile page is scoped to the *session* identity, so they
attack it with foreign ids, tenant ids and parameter injection as well as checking
the happy path.
"""
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import Member, Organisation, UserAccess
from datetime import date

STORAGE = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
           'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class AccountTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.secretary = User.objects.create_user('sec', password='A-Fresh-Strong-Password!', email='sec@example.com')
        UserAccess.objects.create(organization=self.org, user=self.secretary, role='secretary')
        self.auditor = User.objects.create_user('auditor', password='A-Fresh-Strong-Password!', email='auditor@example.com')
        UserAccess.objects.create(organization=self.org, user=self.auditor, role='auditor')
        self.member = Member.objects.create(organization=self.org, full_name='Sample', joined=date(2026, 9, 1))
        # A second tenant, used to prove the profile page never leaks across orgs.
        self.rival_org = Organisation.objects.create(name='Rival Organization')
        self.rival = User.objects.create_user('rival', password='A-Fresh-Strong-Password!', email='rival@example.com')
        UserAccess.objects.create(organization=self.rival_org, user=self.rival, role='secretary')

    def post(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')


class ProfilePageTests(AccountTests):
    def test_profile_route_serves_the_application_shell(self):
        self.client.force_login(self.secretary)
        response = self.client.get('/profile/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<div id="root">', response.content)

    def test_profile_requires_authentication(self):
        response = self.client.get('/profile/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/'))

    def test_profile_denied_without_active_membership(self):
        # Authenticated but membership was revoked, which must not open the shell.
        self.client.force_login(self.rival)
        self.rival.access.active = False
        self.rival.access.save(update_fields=['active'])
        self.assertEqual(self.client.get('/profile/').status_code, 403)

    def test_profile_ignores_a_frontend_supplied_user_id(self):
        # /profile/ takes no id. A spoofed one must not change who is displayed.
        self.client.force_login(self.secretary)
        response = self.client.get('/profile/', {'user_id': self.rival.pk, 'id': self.rival.pk, 'pk': self.rival.pk})
        self.assertEqual(response.status_code, 200)
        session = self.client.get('/api/session/').json()
        self.assertEqual(session['user_id'], self.secretary.pk)
        self.assertEqual(session['organisation'], self.org.name)

    def test_profile_uses_the_session_identity_for_its_data(self):
        self.client.force_login(self.auditor)
        response = self.client.get('/profile/')
        self.assertEqual(response.status_code, 200)
        session = self.client.get('/api/session/').json()
        self.assertEqual(session['user_id'], self.auditor.pk)
        self.assertEqual(session['role'], 'auditor')
        self.assertEqual(session['organisation'], self.org.name)
        # The page must not render the other tenant's name anywhere.
        self.assertNotContains(response, self.rival_org.name)

    def test_profile_is_reversible_from_the_shell_root(self):
        self.client.force_login(self.secretary)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(reverse('profile'), '/profile/')


class SessionInfoTests(AccountTests):
    def test_anonymous_cannot_read_the_current_user(self):
        response = self.client.get('/api/session/')
        self.assertEqual(response.status_code, 401)
        self.assertIn('error', response.json())

    def test_session_reports_only_the_callers_own_account(self):
        self.client.force_login(self.auditor)
        data = self.client.get('/api/session/').json()
        self.assertEqual(data['user_id'], self.auditor.pk)
        self.assertEqual(data['organisation'], self.org.name)
        self.assertEqual(data['role'], 'auditor')
        self.assertIn('username', data)
        self.assertIn('branding', data)

    def test_session_sets_the_csrf_cookie_the_app_relies_on(self):
        self.client.force_login(self.secretary)
        self.assertIn('csrftoken', self.client.get('/api/session/').cookies)

    def test_session_rejects_a_repeat_of_a_different_user_id(self):
        self.client.force_login(self.secretary)
        self.assertEqual(self.post('/api/session/', {'user_id': self.rival.pk}).status_code, 405)


class LogoutTests(AccountTests):
    def test_logout_invalidates_the_session(self):
        self.client.force_login(self.secretary)
        self.assertEqual(self.client.get('/api/session/').status_code, 200)
        response = self.client.post('/logout/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/'))
        # The invalidated session must not be able to reach authenticated API.
        self.assertEqual(self.client.get('/api/session/').status_code, 401)
        self.assertEqual(self.client.get('/api/members/').status_code, 401)

    def test_logout_is_post_only(self):
        self.client.force_login(self.secretary)
        # GET must not be able to end a session, so a stray link cannot sign a user out.
        self.assertEqual(self.client.get('/logout/').status_code, 405)
        self.assertEqual(self.client.get('/api/session/').status_code, 200)

    def test_logout_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.secretary)
        self.assertEqual(csrf_client.post('/logout/').status_code, 403)
        # Still signed in, because the rejected request never reached the view.
        self.assertEqual(csrf_client.get('/api/session/').status_code, 200)

    def test_logout_when_already_signed_out_is_harmless(self):
        response = self.client.post('/logout/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get('/api/session/').status_code, 401)

    def test_logout_does_not_affect_another_users_session(self):
        other = Client()
        other.force_login(self.auditor)
        self.client.force_login(self.secretary)
        self.client.post('/logout/')
        self.assertEqual(other.get('/api/session/').status_code, 200)


class PasswordChangeTests(AccountTests):
    def test_valid_password_change_succeeds_and_keeps_the_current_session(self):
        self.client.force_login(self.secretary)
        response = self.client.post('/account/password/', {
            'old_password': 'A-Fresh-Strong-Password!',
            'new_password1': 'An-Entirely-Different-Phrase-91!',
            'new_password2': 'An-Entirely-Different-Phrase-91!',
        })
        self.assertEqual(response.status_code, 302, response.content[:400])
        self.assertTrue(response['Location'].startswith('/account/password/done/'))
        # PasswordChangeView calls update_session_auth_hash, so the caller stays
        # signed in on this device. Guard that, since a regression here silently
        # signs the user out mid-change.
        self.assertEqual(self.client.get('/api/session/').status_code, 200)
        self.secretary.refresh_from_db()
        self.assertTrue(self.secretary.check_password('An-Entirely-Different-Phrase-91!'))

    def test_wrong_current_password_is_rejected(self):
        self.client.force_login(self.secretary)
        response = self.client.post('/account/password/', {
            'old_password': 'not-the-password',
            'new_password1': 'An-Entirely-Different-Phrase-91!',
            'new_password2': 'An-Entirely-Different-Phrase-91!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'old password was entered incorrectly')
        self.secretary.refresh_from_db()
        self.assertTrue(self.secretary.check_password('A-Fresh-Strong-Password!'))

    def test_weak_new_password_is_rejected_by_the_existing_validators(self):
        self.client.force_login(self.secretary)
        response = self.client.post('/account/password/', {
            'old_password': 'A-Fresh-Strong-Password!',
            'new_password1': '1234',
            'new_password2': '1234',
        })
        self.assertEqual(response.status_code, 200)
        self.secretary.refresh_from_db()
        self.assertTrue(self.secretary.check_password('A-Fresh-Strong-Password!'))

    def test_password_change_requires_authentication(self):
        response = self.client.post('/account/password/', {
            'old_password': 'A-Fresh-Strong-Password!',
            'new_password1': 'An-Entirely-Different-Phrase-91!',
            'new_password2': 'An-Entirely-Different-Phrase-91!',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/'))

    def test_password_change_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.secretary)
        self.assertEqual(csrf_client.post('/account/password/', {
            'old_password': 'A-Fresh-Strong-Password!',
            'new_password1': 'An-Entirely-Different-Phrase-91!',
            'new_password2': 'An-Entirely-Different-Phrase-91!',
        }).status_code, 403)
        self.secretary.refresh_from_db()
        self.assertTrue(self.secretary.check_password('A-Fresh-Strong-Password!'))


class AccountIsolationTests(AccountTests):
    def test_accounts_listing_stays_inside_the_tenant(self):
        self.client.force_login(self.secretary)
        usernames = {u['username'] for u in self.client.get('/api/accounts/').json()['users']}
        self.assertEqual(usernames, {'sec', 'auditor'})
        self.assertNotIn('rival', usernames)

    def test_auditor_cannot_reach_account_administration(self):
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get('/api/accounts/').status_code, 403)
        self.assertEqual(self.post('/api/accounts/', {
            'username': 'x', 'email': 'x@example.com', 'password': 'Another-Strong-Phrase-42!', 'role': 'auditor',
        }).status_code, 403)

    def test_auditor_cannot_disable_another_account(self):
        self.client.force_login(self.auditor)
        self.assertEqual(self.post(f'/api/accounts/{self.secretary.pk}/disable/', {}).status_code, 403)
        self.assertTrue(UserAccess.objects.get(user=self.secretary).active)
