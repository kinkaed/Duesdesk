"""The manual sweep that closes abandoned signup workspaces.

Immediate creation means an abandoned signup leaves a real organization behind, so
this is the one place it can be stopped. The command is deliberately conservative:
"no members yet" is not enough, because a workspace can be real before its first
member is entered.
"""
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import AuditEvent, Organisation, SecretaryInvite, UserAccess

FASTER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


@FASTER
class DisableStaleOrganizationsTests(TestCase):
    def make_org(self, name='Stale Co', age_days=40, onboarded=False, role='secretary',
                 active=True):
        org = Organisation.objects.create(name=name, onboarding_required=not onboarded)
        if onboarded:
            org.onboarded_at = timezone.now()
            org.save(update_fields=['onboarded_at'])
        Organisation.objects.filter(pk=org.pk).update(
            created_at=timezone.now() - timedelta(days=age_days))
        user = User.objects.create_user(f'user-{org.pk}')
        UserAccess.objects.create(user=user, organization=org, role=role, active=active)
        return org

    def run_command(self, *args):
        output = StringIO()
        call_command('disable_stale_organizations', *args, stdout=output)
        return output.getvalue()

    def test_an_abandoned_workspace_is_disabled_and_audited(self):
        org = self.make_org()

        self.run_command()

        org.refresh_from_db()
        self.assertIsNotNone(org.disabled_at)
        event = AuditEvent.objects.get(action='organization.disabled')
        self.assertEqual(event.organization, org)
        self.assertEqual(event.reason, 'stale_onboarding')

    def test_an_organization_with_a_member_is_never_touched(self):
        org = self.make_org()
        from .models import Member
        Member.objects.create(organization=org, full_name='First member', joined=timezone.localdate())

        self.run_command()

        org.refresh_from_db()
        self.assertIsNone(org.disabled_at)

    def test_an_organization_with_an_accepted_invitation_is_never_touched(self):
        org = self.make_org()
        inviter = UserAccess.objects.get(organization=org).user
        SecretaryInvite.objects.create(organization=org, email='past@example.com',
                                       created_by=inviter, token_hash='x' * 64,
                                       expires_at=timezone.now(),
                                       used_at=timezone.now())

        self.run_command()

        org.refresh_from_db()
        self.assertIsNone(org.disabled_at)

    def test_a_legacy_organization_is_never_treated_as_abandoned(self):
        org = self.make_org(onboarded=True)

        self.run_command()

        org.refresh_from_db()
        self.assertIsNone(org.disabled_at)

    def test_an_organization_that_has_been_used_by_two_people_is_never_touched(self):
        org = self.make_org()
        second = User.objects.create_user('second-secretary')
        UserAccess.objects.create(user=second, organization=org, role='secretary')

        self.run_command()

        org.refresh_from_db()
        self.assertIsNone(org.disabled_at)

    def test_a_recent_organization_is_left_alone(self):
        org = self.make_org(age_days=1)

        self.run_command()

        org.refresh_from_db()
        self.assertIsNone(org.disabled_at)

    def test_dry_run_writes_nothing(self):
        org = self.make_org()

        output = self.run_command('--dry-run')

        self.assertIn('Would disable', output)
        org.refresh_from_db()
        self.assertIsNone(org.disabled_at)
        self.assertFalse(AuditEvent.objects.filter(action='organization.disabled').exists())

    def test_a_second_run_disables_nothing_new(self):
        org = self.make_org()
        self.run_command()
        org.refresh_from_db()
        first = org.disabled_at

        output = self.run_command()

        self.assertIn('Disabled 0 organization(s)', output)
        org.refresh_from_db()
        self.assertEqual(org.disabled_at, first)
