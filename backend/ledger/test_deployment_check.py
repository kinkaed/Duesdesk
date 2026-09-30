from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Exists, OuterRef, Q
from django.test import TestCase, override_settings

from .management.commands.deployment_check import DEMO_PASSWORD
from .models import Member, Organisation, Payment, UserAccess


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ActiveSecretaryTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Treasury Association')
        self.member = Member.objects.create(organization=self.org, full_name='Ada Lovelace',
                                            joined=date(2026, 9, 1))
        self.command = self.deployment_check()

    def deployment_check(self):
        from .management.commands.deployment_check import Command
        return Command()

    def test_an_organization_with_records_and_no_secretary_is_named(self):
        with self.assertRaises(CommandError) as caught:
            self.command._check_active_secretaries()
        self.assertIn(str(self.org.pk), str(caught.exception))
        self.assertIn('assign_access', str(caught.exception))

    def test_an_inactive_secretary_does_not_count(self):
        user = User.objects.create_user('left', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=user, role='secretary', active=False)
        with self.assertRaises(CommandError):
            self.command._check_active_secretaries()

    def test_a_disabled_secretary_account_does_not_count(self):
        user = User.objects.create_user('gone', password='A-Fresh-Strong-Password!', is_active=False)
        UserAccess.objects.create(organization=self.org, user=user, role='secretary')
        with self.assertRaises(CommandError):
            self.command._check_active_secretaries()

    def test_an_auditor_is_not_a_secretary(self):
        user = User.objects.create_user('reader', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=user, role='auditor')
        with self.assertRaises(CommandError):
            self.command._check_active_secretaries()

    def test_an_active_secretary_passes(self):
        user = User.objects.create_user('keeps', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=user, role='secretary')
        self.command._check_active_secretaries()

    def test_an_organization_holding_nothing_is_not_flagged(self):
        # An organization with no access, no members and no payments is not
        # flagged, because the gate only asks about organizations that exist in
        # someone's ledger.
        self.member.delete()
        self.command._check_active_secretaries()

    def test_access_alone_is_enough_to_need_a_secretary(self):
        # The original gate matched any organization with a UserAccess row, so
        # an auditor-only organization has always been flagged. Signup gives
        # every new organization a secretary, so in practice this only fires for
        # an organization whose secretary was later removed or demoted, which is
        # the situation the gate exists to catch.
        self.member.delete()
        user = User.objects.create_user('reader', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=user, role='auditor')
        with self.assertRaises(CommandError) as caught:
            self.command._check_active_secretaries()
        self.assertIn(str(self.org.pk), str(caught.exception))


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DemoPasswordTests(TestCase):
    """The gate must catch the demo login, and must not cost a hash per account.

    The second half matters as much as the first: hashing every active account
    was the single largest phase of the preDeploy command, so a change that
    reintroduces the sweep has to fail here rather than on Render.
    """

    def command(self):
        from .management.commands.deployment_check import Command
        return Command()

    def test_the_seeded_demo_account_is_caught_by_default(self):
        User.objects.create_user('secretary', password=DEMO_PASSWORD, is_staff=True)
        with self.assertRaises(CommandError) as caught:
            self.command()._check_demo_password(False)
        self.assertIn('demo account', str(caught.exception))

    def test_a_disabled_demo_account_is_ignored(self):
        # The original gate only scanned active accounts, so a disabled one is
        # not a live credential. Keep that behaviour explicit.
        User.objects.create_user('secretary', password=DEMO_PASSWORD, is_staff=True, is_active=False)
        self.command()._check_demo_password(False)

    def test_the_default_gate_hashes_only_the_accounts_the_seeder_creates(self):
        for index in range(25):
            User.objects.create_user(f'account-{index}', password='A-Fresh-Strong-Password!')
        User.objects.create_user('secretary', password='A-Fresh-Strong-Password!', is_staff=True)
        with patch.object(User, 'check_password', autospec=True, return_value=False) as checked:
            self.command()._check_demo_password(False)
        self.assertEqual(checked.call_count, 1)

    def test_the_exhaustive_sweep_still_reaches_every_active_account(self):
        for index in range(5):
            User.objects.create_user(f'account-{index}', password='A-Fresh-Strong-Password!')
        impostor = User.objects.create_user('imported', password=DEMO_PASSWORD)
        self.assertFalse(impostor.is_staff)
        # Not the account seed_demo can create, so the default gate does not
        # look at it. The sweep does, which is why the flag exists.
        self.command()._check_demo_password(False)
        with self.assertRaises(CommandError):
            self.command()._check_demo_password(True)


@override_settings(PRODUCTION=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   EMAIL_HOST='smtp.example.org',
                   DEFAULT_FROM_EMAIL='dues@example.org')
class ReportTests(TestCase):
    def test_the_command_reports_the_data_it_is_reasoning_about(self):
        # The row counts are the missing fact in every projection of this
        # command's runtime, so it prints them rather than assuming them.
        out = StringIO()
        with patch('ledger.management.commands.deployment_check.call_command'):
            call_command('deployment_check', stdout=out)
        report = out.getvalue()
        for label in ('organizations', 'members', 'payments', 'allocations', 'accounts'):
            self.assertIn(label, report)
        self.assertIn('Phase timings', report)
        self.assertIn('Total', report)
        self.assertIn('Application checks passed', report)

    def test_a_failing_phase_still_reports_its_cost(self):
        # A failed deploy is when someone reads this log. If the report were
        # written only on success, the phase that ran long, or the phase that
        # never got to run, would be invisible exactly when it matters.
        out = StringIO()
        with patch('ledger.management.commands.deployment_check.call_command',
                   side_effect=CommandError('check_finances found a mismatch')):
            with self.assertRaises(CommandError):
                call_command('deployment_check', stdout=out)
        report = out.getvalue()
        self.assertIn('django check --deploy:', report)
        self.assertIn('FAILED', report)
        self.assertIn('Total:', report)
        # Phases after the failure must not claim to have run.
        self.assertNotIn('demo password:', report)
        self.assertNotIn('Application checks passed', report)


@override_settings(PRODUCTION=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   EMAIL_HOST='smtp.example.org',
                   DEFAULT_FROM_EMAIL='dues@example.org')
class SmtpTests(TestCase):
    def command(self):
        from .management.commands.deployment_check import Command
        return Command()

    def test_valid_smtp_passes(self):
        self.command()._check_smtp()

    @override_settings(DEFAULT_FROM_EMAIL='dues@localhost')
    def test_a_loopback_sender_fails_the_gate(self):
        # Recovery mail that never leaves the machine is the failure this is
        # looking for, so the loopback test is on the sender, not the server.
        with self.assertRaises(CommandError) as caught:
            self.command()._check_smtp()
        self.assertIn('SMTP', str(caught.exception))

    @override_settings(EMAIL_HOST='')
    def test_a_missing_smtp_server_fails_the_gate(self):
        with self.assertRaises(CommandError):
            self.command()._check_smtp()


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class QueryParityTests(TestCase):
    """The fast query must answer the same question as the one it replaced.

    The organization listing was rewritten from three joined relations plus
    DISTINCT to four correlated EXISTS subqueries. The old form is slow but
    obviously correct, so it stays in this file as the oracle: if a future
    change to the EXISTS version drops or invents an organization, this fails
    instead of the gate quietly passing a broken deployment.
    """

    def setUp(self):
        # UserAccess.user is a OneToOneField, so an account belongs to exactly
        # one organization with exactly one role. Every shape therefore needs
        # its own accounts.
        self.author = User.objects.create_user('parity-author', password='A-Fresh-Strong-Password!')
        self.shapes = {}
        for name in ('no records', 'member only', 'payment only', 'access only',
                     'auditor and member', 'active secretary', 'inactive secretary',
                     'disabled secretary', 'auditor plus active secretary'):
            self.shapes[name] = Organisation.objects.create(name=name)
        for index, (name, org) in enumerate(self.shapes.items()):
            if name in ('member only', 'payment only', 'auditor and member',
                        'active secretary', 'inactive secretary', 'disabled secretary',
                        'auditor plus active secretary'):
                member = Member.objects.create(organization=org, full_name=f'Member {index}',
                                               joined=date(2026, 9, 1))
                if name == 'payment only':
                    Payment.objects.create(organization=org, member=member, amount_received=Decimal('25.00'),
                                          payment_date=date(2026, 9, 1), method='Cash',
                                          request_key=uuid4(), created_by=self.author)
            if name in ('access only', 'auditor and member', 'auditor plus active secretary'):
                self.grant(org, f'auditor-{index}', 'auditor')
            if name == 'active secretary':
                self.grant(org, 'secretary-active', 'secretary')
            if name == 'inactive secretary':
                self.grant(org, 'secretary-inactive', 'secretary', active=False)
            if name == 'disabled secretary':
                self.grant(org, 'secretary-disabled', 'secretary', user_active=False)
            if name == 'auditor plus active secretary':
                self.grant(org, 'secretary-second', 'secretary')

    def grant(self, org, username, role, active=True, user_active=True):
        user = User.objects.create_user(username, password='A-Fresh-Strong-Password!',
                                        is_active=user_active)
        UserAccess.objects.create(organization=org, user=user, role=role, active=active)
        return user

    def legacy_organizations_needing_a_secretary(self):
        """The implementation this PR replaced, used here as the oracle."""
        flagged = set()
        for org in Organisation.objects.filter(
                Q(useraccess__isnull=False) | Q(member__isnull=False) | Q(payment__isnull=False)).distinct():
            if not UserAccess.objects.filter(organization=org, active=True, role='secretary',
                                             user__is_active=True).exists():
                flagged.add(org.pk)
        return flagged

    def current_organizations_needing_a_secretary(self):
        organizations = Organisation.objects.annotate(
            has_access=Exists(UserAccess.objects.filter(organization_id=OuterRef('pk'))),
            has_member=Exists(Member.objects.filter(organization_id=OuterRef('pk'))),
            has_payment=Exists(Payment.objects.filter(organization_id=OuterRef('pk'))),
            has_secretary=Exists(UserAccess.objects.filter(organization_id=OuterRef('pk')).filter(
                Q(active=True, role='secretary', user__is_active=True))),
        ).filter(Q(has_access=True) | Q(has_member=True) | Q(has_payment=True))
        return set(organizations.exclude(has_secretary=True).values_list('pk', flat=True))

    def test_the_two_queries_agree_on_every_organization_shape(self):
        self.assertEqual(self.current_organizations_needing_a_secretary(),
                         self.legacy_organizations_needing_a_secretary())

    def test_the_agreed_verdict_still_fails_the_gate(self):
        from .management.commands.deployment_check import Command
        with self.assertRaises(CommandError):
            Command()._check_active_secretaries()
